"""SqliteMessageBus tests."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.adapters.message_bus import SqliteMessageBus
from virtual_dev.domain.ports.message_bus import AgentMessage


def _msg(to: str = "analyst", topic: str = "task.discovered", **payload: object) -> AgentMessage:
    return AgentMessage(id="", from_agent="orchestrator", to_agent=to, topic=topic, payload=payload)


@pytest.mark.asyncio
async def test_publish_then_subscribe_delivers_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bus = SqliteMessageBus(session_factory, poll_interval_seconds=0.05)
    await bus.publish(_msg(topic="task.discovered", task_id="DM-1"))

    agen = bus.subscribe("analyst")
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
    agen = bus.subscribe("analyst")
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
    agen = bus.subscribe("analyst")

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
    bus.subscribe("analyst")
    bus.subscribe("researcher")

    await bus.publish(AgentMessage(id="", from_agent="orchestrator", to_agent="*",
                                   topic="shutdown", payload={}))

    analyst_msg = await bus._claim_next("analyst")
    researcher_msg = await bus._claim_next("researcher")
    assert analyst_msg is not None and analyst_msg.topic == "shutdown"
    assert researcher_msg is not None and researcher_msg.topic == "shutdown"
