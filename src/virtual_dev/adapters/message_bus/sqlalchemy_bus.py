"""SQLAlchemy-backed implementation of :class:`MessageBusPort`.

Works with both PostgreSQL (production, via ``asyncpg``) and SQLite
(tests, via ``aiosqlite``). The only dialect-specific code is the
upsert in ``_register_subscriber`` — both dialects support
``on_conflict_do_update`` with the same API.

Durability + at-least-once via lease/ack:

* Subscribers are recorded in ``bus_subscriptions`` so a broadcast
  (``to_agent="*"``) goes to every agent that *ever* registered, even
  if its consumer process hasn't come up yet — fixes the previous
  in-memory-set behaviour where startup ordering silently dropped
  broadcasts.
* ``_claim_next`` reserves a row by setting ``claimed_until = now +
  lease``; the consumer calls ``ack`` on success which marks
  ``consumed_at = now``. A crashed handler / killed process leaves
  ``claimed_until`` in place; the next poll's lazy reaper resets it
  back to NULL so the message is reclaimable.
* Handler idempotency is the consumer's job. Application models
  already enforce it via UNIQUE constraints (``tasks``,
  ``merge_requests``, ``analyst_conversation_fragments``).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from loguru import logger
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.domain.ports.message_bus import AgentMessage, MessageBusPort
from virtual_dev.infrastructure.db import AgentMessageRow, BusSubscriptionRow

_DEFAULT_POLL_INTERVAL_SECONDS = 1.0
_DEFAULT_LEASE_SECONDS = 300.0
# "*" — synthetic address meaning "fan out to every durable subscriber".
_BROADCAST = "*"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _naive(dt: datetime) -> datetime:
    """Strip tzinfo. SQLite's ``DateTime`` column type stores naive
    datetimes; comparing a tz-aware ``now`` against a row read back as
    naive raises ``TypeError`` from SQLAlchemy's evaluator. We
    normalise everything that touches the DB to naive UTC."""
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


class SqlAlchemyMessageBus(MessageBusPort):
    """Durable message bus backed by SQLAlchemy (PostgreSQL or SQLite)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        dialect_name: str = "sqlite",
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        lease_seconds: float = _DEFAULT_LEASE_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._dialect_name = dialect_name
        self._poll_interval = poll_interval_seconds
        self._lease = timedelta(seconds=lease_seconds)
        self._now = clock or _utcnow
        # In-memory snapshot of subscribers registered in *this* process,
        # populated synchronously by ``subscribe()`` so a broadcast that
        # follows subscribe() in the same task hits the right consumers
        # without waiting for the durable INSERT to flush. Durable state
        # in ``bus_subscriptions`` is the source of truth across restarts.
        self._local_subscribers: set[str] = set()

    # --- publish / subscribe / ack -------------------------------------

    async def publish(self, message: AgentMessage) -> None:
        if message.to_agent == _BROADCAST:
            durable = set(await self._all_subscribers())
            targets = sorted(durable | self._local_subscribers)
            if not targets:
                logger.warning(
                    "Broadcast published for topic={!r} but no subscribers "
                    "have ever registered; dropping.",
                    message.topic,
                )
                return
        else:
            targets = [message.to_agent]

        created_at = _naive(message.created_at or self._now())
        async with self._session_factory() as session:
            for target in targets:
                row = AgentMessageRow(
                    external_id=message.id or uuid.uuid4().hex,
                    from_agent=message.from_agent,
                    to_agent=target,
                    topic=message.topic,
                    payload_json=dict(message.payload),
                    correlation_id=message.correlation_id,
                    created_at=created_at,
                )
                session.add(row)
            await session.commit()

    async def subscribe(self, agent_key: str) -> AsyncIterator[AgentMessage]:
        # In-memory side fixes the same-process race for ad-hoc tests;
        # the durable INSERT makes the registration survive restarts so
        # broadcasts from later processes still find this consumer.
        self._local_subscribers.add(agent_key)
        await self._register_subscriber(agent_key)

        async def _iter() -> AsyncIterator[AgentMessage]:
            while True:
                msg = await self._claim_next(agent_key)
                if msg is None:
                    await asyncio.sleep(self._poll_interval)
                    continue
                yield msg

        return _iter()

    async def ack(self, message: AgentMessage) -> None:
        """Finalise a message by stamping ``consumed_at``. After ack the
        row will not be reclaimable even if its lease was about to
        expire."""
        if message._row_id is None:
            # Defensive: in-memory bus or hand-built message without a
            # row id — nothing to ack.
            return
        async with self._session_factory() as session:
            await session.execute(
                update(AgentMessageRow)
                .where(AgentMessageRow.id == message._row_id)
                .values(consumed_at=_naive(self._now()))
            )
            await session.commit()

    # --- internals ------------------------------------------------------

    def _dialect_insert(self):
        """Return the dialect-specific ``insert`` callable."""
        if self._dialect_name == "postgresql":
            return pg_insert
        return sqlite_insert

    async def _register_subscriber(self, agent_key: str) -> None:
        """Idempotent INSERT OR REPLACE — refreshes ``last_seen_at``."""
        try:
            now = _naive(self._now())
            async with self._session_factory() as session:
                insert_fn = self._dialect_insert()
                stmt = insert_fn(BusSubscriptionRow).values(
                    agent_key=agent_key,
                    registered_at=now,
                    last_seen_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=[BusSubscriptionRow.agent_key],
                    set_={"last_seen_at": stmt.excluded.last_seen_at},
                )
                await session.execute(stmt)
                await session.commit()
        except Exception:
            logger.exception(
                "SqlAlchemyMessageBus: failed to register subscriber {!r}",
                agent_key,
            )

    async def _all_subscribers(self) -> list[str]:
        async with self._session_factory() as session:
            rows = (await session.execute(select(BusSubscriptionRow.agent_key))).scalars().all()
        return list(rows)

    async def _claim_next(self, agent_key: str) -> AgentMessage | None:
        """Atomically pick the oldest free message for ``agent_key``."""
        async with self._session_factory() as session:
            msg = await self._claim_within(session, agent_key)
            if msg is not None:
                await session.commit()
            return msg

    async def _claim_within(self, session: AsyncSession, agent_key: str) -> AgentMessage | None:
        now = _naive(self._now())
        # "Free" = not consumed and either never claimed or lease expired.
        free = AgentMessageRow.consumed_at.is_(None) & or_(
            AgentMessageRow.claimed_until.is_(None),
            AgentMessageRow.claimed_until <= now,
        )
        stmt = (
            select(AgentMessageRow)
            .where(AgentMessageRow.to_agent == agent_key, free)
            .order_by(AgentMessageRow.created_at.asc())
            .limit(1)
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None

        # Atomic lease: only succeed if the row is still free in the same
        # transaction — defends against another consumer claiming first.
        result = await session.execute(
            update(AgentMessageRow)
            .where(
                AgentMessageRow.id == row.id,
                AgentMessageRow.consumed_at.is_(None),
                or_(
                    AgentMessageRow.claimed_until.is_(None),
                    AgentMessageRow.claimed_until <= now,
                ),
            )
            .values(claimed_until=now + self._lease)
        )
        if result.rowcount == 0:
            return None

        return AgentMessage(
            id=row.external_id,
            from_agent=row.from_agent,
            to_agent=row.to_agent,
            topic=row.topic,
            payload=cast(dict[str, Any], row.payload_json or {}),
            correlation_id=row.correlation_id,
            created_at=row.created_at,
            _row_id=row.id,
        )


# Backward-compatible alias for tests / imports that haven't migrated yet.
SqliteMessageBus = SqlAlchemyMessageBus
