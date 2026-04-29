"""SqliteMessageBus tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.adapters.message_bus import SqliteMessageBus
from virtual_dev.domain.ports.message_bus import AgentMessage
from virtual_dev.infrastructure.db import Base, make_engine, make_session_factory


def _msg(to: str = "analyst", topic: str = "task.discovered", **payload: object) -> AgentMessage:
    return AgentMessage(id="", from_agent="orchestrator", to_agent=to, topic=topic, payload=payload)


class _Clock:
    """Mutable clock for testing lease behaviour without sleeping."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, *, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


@pytest.fixture
async def file_session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """File-backed SQLite so two bus instances can share durable state.
    In-memory SQLite is per-connection, which defeats the cross-restart
    test."""
    db = tmp_path / "bus.db"
    engine = make_engine(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_publish_then_subscribe_delivers_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bus = SqliteMessageBus(session_factory, poll_interval_seconds=0.05)
    await bus.publish(_msg(topic="task.discovered", task_id="DM-1"))

    agen = await bus.subscribe("analyst")
    msg = await asyncio.wait_for(anext(agen), timeout=2)  # type: ignore[name-defined]
    assert msg.topic == "task.discovered"
    assert msg.payload["task_id"] == "DM-1"


@pytest.mark.asyncio
async def test_messages_are_claimed_only_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bus = SqliteMessageBus(session_factory, poll_interval_seconds=0.05)
    for i in range(3):
        await bus.publish(_msg(topic="task.discovered", task_id=f"DM-{i}"))

    # First subscriber drains all three.
    received: list[str] = []
    agen = await bus.subscribe("analyst")
    for _ in range(3):
        msg = await asyncio.wait_for(anext(agen), timeout=2)  # type: ignore[name-defined]
        received.append(msg.payload["task_id"])
    assert received == ["DM-0", "DM-1", "DM-2"]

    # Nothing left for a fresh claim.
    assert await bus._claim_next("analyst") is None


@pytest.mark.asyncio
async def test_subscribe_for_unknown_key_blocks_until_published(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bus = SqliteMessageBus(session_factory, poll_interval_seconds=0.05)
    agen = await bus.subscribe("analyst")

    async def _produce() -> None:
        await asyncio.sleep(0.1)
        await bus.publish(_msg(topic="task.discovered", task_id="DM-42"))

    producer = asyncio.create_task(_produce())
    msg = await asyncio.wait_for(anext(agen), timeout=2)  # type: ignore[name-defined]
    await producer
    assert msg.payload["task_id"] == "DM-42"


@pytest.mark.asyncio
async def test_broadcast_fans_out_to_known_subscribers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bus = SqliteMessageBus(session_factory, poll_interval_seconds=0.05)

    # Subscribers must exist before the broadcast (see adapter docstring).
    await bus.subscribe("analyst")
    await bus.subscribe("researcher")

    await bus.publish(AgentMessage(id="", from_agent="orchestrator", to_agent="*",
                                   topic="shutdown", payload={}))

    analyst_msg = await bus._claim_next("analyst")
    researcher_msg = await bus._claim_next("researcher")
    assert analyst_msg is not None and analyst_msg.topic == "shutdown"
    assert researcher_msg is not None and researcher_msg.topic == "shutdown"


# --- durable subscribers + lease/ack -------------------------------------


@pytest.mark.asyncio
async def test_message_redelivered_when_handler_does_not_ack(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Claim takes a lease (claimed_until = now + lease_seconds) instead
    of marking ``consumed_at`` immediately. If the consumer never calls
    ack and the lease expires, the message must come back so a crashed
    handler doesn't silently drop work."""
    clock = _Clock()
    bus = SqliteMessageBus(
        session_factory,
        poll_interval_seconds=0.01,
        lease_seconds=300,
        clock=clock.now,
    )
    await bus.publish(_msg(topic="task.discovered", task_id="DM-1"))

    first = await bus._claim_next("analyst")
    assert first is not None and first.payload["task_id"] == "DM-1"

    # No ack. Within the lease window the message stays "in flight".
    inside = await bus._claim_next("analyst")
    assert inside is None, "lease still valid — must not redeliver"

    # Past the lease the reaper should make it claimable again.
    clock.advance(seconds=600)
    redelivered = await bus._claim_next("analyst")
    assert redelivered is not None
    assert redelivered.payload["task_id"] == "DM-1"


@pytest.mark.asyncio
async def test_acked_message_is_not_redelivered(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = _Clock()
    bus = SqliteMessageBus(
        session_factory,
        poll_interval_seconds=0.01,
        lease_seconds=300,
        clock=clock.now,
    )
    await bus.publish(_msg(topic="task.discovered", task_id="DM-1"))

    msg = await bus._claim_next("analyst")
    assert msg is not None
    await bus.ack(msg)

    # Even far past the lease, an acked message stays gone.
    clock.advance(seconds=10_000)
    assert await bus._claim_next("analyst") is None


@pytest.mark.asyncio
async def test_broadcast_survives_subscriber_restart(
    file_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A subscriber that registered in a previous bus instance (process)
    must still receive broadcasts, even if the new instance hasn't yet
    re-called subscribe(). Achieved by persisting the subscriber set."""
    bus_first = SqliteMessageBus(file_session_factory, poll_interval_seconds=0.01)
    await bus_first.subscribe("analyst")

    # Drop the first bus — simulate process restart. A fresh bus on the
    # same DB shouldn't lose the analyst's identity.
    bus_second = SqliteMessageBus(file_session_factory, poll_interval_seconds=0.01)
    await bus_second.publish(AgentMessage(
        id="", from_agent="orchestrator", to_agent="*",
        topic="shutdown", payload={},
    ))

    msg = await bus_second._claim_next("analyst")
    assert msg is not None
    assert msg.topic == "shutdown"


@pytest.mark.asyncio
async def test_lease_reaper_returns_expired_claims(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Explicit form of the redelivery test — focuses on the reaper
    behaviour: ``claimed_until`` in the past + ``consumed_at IS NULL``
    must come back into the unclaimed pool on the next poll."""
    clock = _Clock()
    bus = SqliteMessageBus(
        session_factory,
        poll_interval_seconds=0.01,
        lease_seconds=60,
        clock=clock.now,
    )
    await bus.publish(_msg(task_id="DM-X"))
    await bus.publish(_msg(task_id="DM-Y"))

    first = await bus._claim_next("analyst")
    second = await bus._claim_next("analyst")
    assert first is not None and second is not None

    # Both leased; nothing free.
    assert await bus._claim_next("analyst") is None

    clock.advance(seconds=120)
    # Both leases expired; reaper hands them back.
    a = await bus._claim_next("analyst")
    b = await bus._claim_next("analyst")
    assert {a.payload["task_id"], b.payload["task_id"]} == {"DM-X", "DM-Y"}  # type: ignore[union-attr]
