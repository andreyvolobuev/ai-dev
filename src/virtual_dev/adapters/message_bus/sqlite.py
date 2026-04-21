"""SQLite-backed implementation of :class:`MessageBusPort`.

The bus uses the existing ``agent_messages`` table as durable storage. New
messages are inserted with ``consumed_at = NULL``; subscribers poll for their
own pending messages, claim them atomically (``consumed_at`` stamped inside
the same transaction), and yield them.

This is intentionally minimal:
    * Single consumer per ``to_agent`` value is assumed (no fan-out inside one
      key — fan-out is done by publishing to ``"*"`` which is expanded here).
    * Ordering is per ``created_at`` ASC.
    * No ack/retry: once ``consumed_at`` is stamped, the message is gone.
      Crash between yield and handler completion = lost message. Acceptable in
      Phase 1; tighten to a proper ack protocol when Dev agents go live.

The point of this adapter is *durability* — messages survive a restart — and
*replaceability* — swap for Redis/RabbitMQ later without touching agents.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, cast

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.domain.ports.message_bus import AgentMessage, MessageBusPort
from virtual_dev.infrastructure.db import AgentMessageRow

_DEFAULT_POLL_INTERVAL_SECONDS = 1.0
# "*" — synthetic address meaning "fan out to all known subscribers".
_BROADCAST = "*"


class SqliteMessageBus(MessageBusPort):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._poll_interval = poll_interval_seconds
        self._known_subscribers: set[str] = set()

    async def publish(self, message: AgentMessage) -> None:
        if message.to_agent == _BROADCAST:
            targets = list(self._known_subscribers)
            if not targets:
                logger.debug(
                    "Broadcast published for topic={!r} but no subscribers yet; "
                    "message is stored only under '*' and lost for any live loops. "
                    "Enrol subscribers before broadcasting.",
                    message.topic,
                )
        else:
            targets = [message.to_agent]

        async with self._session_factory() as session:
            for target in targets:
                row = AgentMessageRow(
                    external_id=message.id or uuid.uuid4().hex,
                    from_agent=message.from_agent,
                    to_agent=target,
                    topic=message.topic,
                    payload_json=dict(message.payload),
                    correlation_id=message.correlation_id,
                    created_at=message.created_at or datetime.now(timezone.utc),
                )
                session.add(row)
            await session.commit()

    def subscribe(self, agent_key: str) -> AsyncIterator[AgentMessage]:
        self._known_subscribers.add(agent_key)

        async def _iter() -> AsyncIterator[AgentMessage]:
            while True:
                msg = await self._claim_next(agent_key)
                if msg is None:
                    await asyncio.sleep(self._poll_interval)
                    continue
                yield msg

        return _iter()

    async def _claim_next(self, agent_key: str) -> AgentMessage | None:
        """Atomically pick the oldest un-consumed message for ``agent_key``."""
        async with self._session_factory() as session:
            msg = await self._claim_within(session, agent_key)
            if msg is not None:
                await session.commit()
            return msg

    async def _claim_within(
        self, session: AsyncSession, agent_key: str
    ) -> AgentMessage | None:
        stmt = (
            select(AgentMessageRow)
            .where(
                AgentMessageRow.to_agent == agent_key,
                AgentMessageRow.consumed_at.is_(None),
            )
            .order_by(AgentMessageRow.created_at.asc())
            .limit(1)
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None

        # Atomic claim: only if still unclaimed (defensive; single-consumer
        # today but keeps invariants if that changes).
        result = await session.execute(
            update(AgentMessageRow)
            .where(
                AgentMessageRow.id == row.id,
                AgentMessageRow.consumed_at.is_(None),
            )
            .values(consumed_at=datetime.now(timezone.utc))
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
        )
