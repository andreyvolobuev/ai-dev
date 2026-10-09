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


# --- ack-after-success protocol -----------------------------------------


class _RecordingBus:
    """Minimal MessageBusPort substitute that records ack() calls and
    yields a single fixed message on subscribe. Sufficient for testing
    the runner's ack contract without booting SQLite."""

    def __init__(self, messages: list[AgentMessage]) -> None:
        self._messages = list(messages)
        self.acked: list[AgentMessage] = []

    async def publish(self, message: AgentMessage) -> None:  # pragma: no cover
        self._messages.append(message)

    async def subscribe(self, agent_key: str):  # type: ignore[no-untyped-def]
        async def _gen():
            for m in self._messages:
                yield m
            # Block forever after the queued messages are drained — same
            # shape as the real bus, lets the runner sit idle until stop.
            while True:
                await asyncio.sleep(60)

        return _gen()

    async def ack(self, message: AgentMessage) -> None:
        self.acked.append(message)

    async def renew(self, message: AgentMessage) -> bool:
        return True


@pytest.mark.asyncio
async def test_runner_acks_after_successful_handler() -> None:
    msg = AgentMessage(
        id="m1", from_agent="orchestrator", to_agent="analyst",
        topic="task.discovered", payload={"task_id": "DM-77"},
    )
    bus = _RecordingBus([msg])

    async def handler(_msg: AgentMessage) -> None:
        return

    runner = AgentRunner(
        agent_key="analyst", message_bus=bus,  # type: ignore[arg-type]
        handlers={"task.discovered": handler},
    )
    task = asyncio.create_task(runner.run_forever())
    try:
        for _ in range(40):
            if bus.acked:
                break
            await asyncio.sleep(0.02)
    finally:
        await runner.stop()
        await asyncio.wait_for(task, timeout=2)

    assert [a.id for a in bus.acked] == ["m1"]


@pytest.mark.asyncio
async def test_runner_does_not_ack_when_handler_raises() -> None:
    """A failed handler must NOT ack — the lease will then expire and
    the bus redelivers, preserving at-least-once semantics."""
    msg = AgentMessage(
        id="m1", from_agent="orchestrator", to_agent="analyst",
        topic="task.discovered", payload={},
    )
    bus = _RecordingBus([msg])

    async def handler(_msg: AgentMessage) -> None:
        raise RuntimeError("boom")

    runner = AgentRunner(
        agent_key="analyst", message_bus=bus,  # type: ignore[arg-type]
        handlers={"task.discovered": handler},
    )
    task = asyncio.create_task(runner.run_forever())
    try:
        for _ in range(40):
            if runner.stats.failed:
                break
            await asyncio.sleep(0.02)
    finally:
        await runner.stop()
        await asyncio.wait_for(task, timeout=2)

    assert runner.stats.failed == 1
    assert bus.acked == []


@pytest.mark.asyncio
async def test_runner_resubscribes_after_bus_iterator_crash() -> None:
    """A transient bus error inside the subscription iterator must not
    kill the runner — it resubscribes and keeps consuming."""
    handled: list[str] = []

    class _FlakyBus:
        def __init__(self) -> None:
            self.subscribe_calls = 0
            self.acked: list[AgentMessage] = []

        async def publish(self, message: AgentMessage) -> None:  # pragma: no cover
            raise AssertionError("not used")

        async def subscribe(self, agent_key: str):  # type: ignore[no-untyped-def]
            self.subscribe_calls += 1
            first = self.subscribe_calls == 1

            async def _gen():
                if first:
                    raise RuntimeError("database is locked")
                yield AgentMessage(
                    id="m1", from_agent="orchestrator", to_agent="analyst",
                    topic="task.discovered", payload={},
                )

            return _gen()

        async def ack(self, message: AgentMessage) -> None:
            self.acked.append(message)

        async def renew(self, message: AgentMessage) -> bool:
            return True

    async def handler(msg: AgentMessage) -> None:
        handled.append(msg.id)

    bus = _FlakyBus()
    runner = AgentRunner(
        agent_key="analyst", message_bus=bus,
        handlers={"task.discovered": handler},
    )
    run = asyncio.create_task(runner.run_forever())
    # First subscription crashes; the runner backs off (5s) — fast-forward
    # by waiting on real time is too slow, so poke the stop-wait through
    # a shortened backoff via monkeypatching would be invasive; instead
    # just wait past the initial 5s backoff bounded by a 8s ceiling.
    for _ in range(80):
        if handled:
            break
        await asyncio.sleep(0.1)
    await runner.stop()
    await asyncio.wait_for(run, timeout=5)

    assert handled == ["m1"]
    assert bus.subscribe_calls >= 2
    assert [m.id for m in bus.acked] == ["m1"]


# --- lease renewal while a handler is in flight --------------------------


@pytest.mark.asyncio
async def test_long_handler_keeps_its_lease(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An analyst run may legitimately take longer than the bus lease
    (code_agent_run_timeout_seconds is 3600 against a 300s lease). While
    the handler is in flight the runner must keep extending the claim,
    or the bus hands the same message out again and the agent re-runs
    the same ticket in parallel — the DM-3548 loop."""
    bus = SqliteMessageBus(
        session_factory, poll_interval_seconds=0.01, lease_seconds=0.3,
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_msg: AgentMessage) -> None:
        started.set()
        await release.wait()

    runner = AgentRunner(
        agent_key="analyst", message_bus=bus,
        handlers={"task.discovered": handler},
        lease_renew_interval_seconds=0.05,
    )
    task = asyncio.create_task(runner.run_forever())
    try:
        await bus.publish(AgentMessage(
            id="", from_agent="orchestrator", to_agent="analyst",
            topic="task.discovered", payload={"task_id": "DM-3548"},
        ))
        await asyncio.wait_for(started.wait(), timeout=2)

        # Three lease-lengths into the handler's work, the claim must
        # still be ours: nobody else may pick this message up.
        await asyncio.sleep(0.9)
        assert await bus._claim_next("analyst") is None

        release.set()
        for _ in range(60):
            if runner.stats.processed:
                break
            await asyncio.sleep(0.02)
    finally:
        release.set()
        await runner.stop()
        await asyncio.wait_for(task, timeout=2)

    assert runner.stats.processed == 1


@pytest.mark.asyncio
async def test_renewal_stops_once_the_handler_fails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Renewal must not outlive the handler: a crashed handler's message
    has to become reclaimable once its lease runs out, which is what
    keeps at-least-once delivery working."""
    bus = SqliteMessageBus(
        session_factory, poll_interval_seconds=0.01, lease_seconds=0.3,
    )

    async def handler(_msg: AgentMessage) -> None:
        raise RuntimeError("boom")

    runner = AgentRunner(
        agent_key="analyst", message_bus=bus,
        handlers={"task.discovered": handler},
        lease_renew_interval_seconds=0.05,
    )
    task = asyncio.create_task(runner.run_forever())
    try:
        await bus.publish(AgentMessage(
            id="", from_agent="orchestrator", to_agent="analyst",
            topic="task.discovered", payload={},
        ))
        for _ in range(60):
            if runner.stats.failed:
                break
            await asyncio.sleep(0.02)
    finally:
        await runner.stop()
        await asyncio.wait_for(task, timeout=2)

    assert runner.stats.failed >= 1
    # The heartbeat died with the handler, so the lease expires.
    await asyncio.sleep(0.4)
    assert await bus._claim_next("analyst") is not None
