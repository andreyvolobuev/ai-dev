"""AgentRunner dispatches bus messages to registered handlers."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.adapters.message_bus import SqliteMessageBus
from virtual_dev.domain.ports.message_bus import AgentMessage
from virtual_dev.runtime.workers import AgentRunner


@pytest.mark.asyncio
async def test_runner_routes_to_registered_handler(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bus = SqliteMessageBus(session_factory, poll_interval_seconds=0.05)
    seen: list[str] = []

    async def handler(msg: AgentMessage) -> None:
        seen.append(msg.payload["task_id"])

    runner = AgentRunner(agent_key="analyst", message_bus=bus, handlers={"task.discovered": handler})
    task = asyncio.create_task(runner.run_forever())

    try:
        await bus.publish(AgentMessage(
            id="", from_agent="orchestrator", to_agent="analyst",
            topic="task.discovered", payload={"task_id": "DM-7"},
        ))
        # Give the loop a moment to drain.
        for _ in range(40):
            if seen:
                break
            await asyncio.sleep(0.05)
    finally:
        await runner.stop()
        await asyncio.wait_for(task, timeout=2)

    assert seen == ["DM-7"]
    assert runner.stats.processed == 1
    assert runner.stats.failed == 0


@pytest.mark.asyncio
async def test_runner_ignores_unknown_topic_without_failing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bus = SqliteMessageBus(session_factory, poll_interval_seconds=0.05)

    async def handler(msg: AgentMessage) -> None:  # pragma: no cover
        raise AssertionError("should not be called")

    runner = AgentRunner(agent_key="analyst", message_bus=bus, handlers={"task.discovered": handler})
    task = asyncio.create_task(runner.run_forever())
    try:
        await bus.publish(AgentMessage(
            id="", from_agent="orchestrator", to_agent="analyst",
            topic="something.else", payload={},
        ))
        await asyncio.sleep(0.2)
    finally:
        await runner.stop()
        await asyncio.wait_for(task, timeout=2)

    assert runner.stats.processed == 0
    assert runner.stats.failed == 0


@pytest.mark.asyncio
async def test_handler_exception_is_logged_not_fatal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bus = SqliteMessageBus(session_factory, poll_interval_seconds=0.05)

    async def handler(msg: AgentMessage) -> None:
        raise RuntimeError("boom")

    runner = AgentRunner(agent_key="analyst", message_bus=bus, handlers={"task.discovered": handler})
    task = asyncio.create_task(runner.run_forever())
    try:
        await bus.publish(AgentMessage(
            id="", from_agent="orchestrator", to_agent="analyst",
            topic="task.discovered", payload={},
        ))
        for _ in range(40):
            if runner.stats.failed:
                break
            await asyncio.sleep(0.05)
    finally:
        await runner.stop()
        await asyncio.wait_for(task, timeout=2)

    assert runner.stats.failed == 1
    assert runner.is_running is False
