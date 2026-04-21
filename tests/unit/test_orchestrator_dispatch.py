"""Orchestrator emits ``task.discovered`` to the bus for newly created tasks."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents import Orchestrator
from virtual_dev.application.agents.orchestrator import (
    AGENT_ANALYST,
    TOPIC_TASK_DISCOVERED,
)
from virtual_dev.domain.models.task import Task, TaskPriority
from virtual_dev.domain.ports.message_bus import AgentMessage, MessageBusPort
from virtual_dev.domain.ports.task_tracker import TaskTrackerPort
from virtual_dev.infrastructure.config.schema import (
    AgentsCfg,
    AppConfig,
    MappingsCfg,
    TaskSourceCfg,
)


class _FakeTracker(TaskTrackerPort):
    def __init__(self, batches: list[list[Task]]) -> None:
        self._batches = batches

    async def fetch_tasks(self, jql: str, limit: int = 50) -> Sequence[Task]:
        return self._batches.pop(0) if self._batches else []

    async def get_task(self, external_id: str) -> Task:  # pragma: no cover
        raise NotImplementedError

    async def transition(self, external_id: str, to_status: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def comment(self, external_id: str, body: str) -> None:  # pragma: no cover
        raise NotImplementedError


class _SpyBus(MessageBusPort):
    def __init__(self) -> None:
        self.published: list[AgentMessage] = []

    async def publish(self, message: AgentMessage) -> None:
        self.published.append(message)

    def subscribe(self, agent_key: str) -> AsyncIterator[AgentMessage]:  # pragma: no cover
        raise NotImplementedError


def _cfg() -> AppConfig:
    return AppConfig(
        repositories=[],
        agents=AgentsCfg(task_source=TaskSourceCfg(jql="x", poll_interval_seconds=60)),
        mappings=MappingsCfg(),
    )


def _task(external_id: str) -> Task:
    return Task(
        external_id=external_id,
        tracker="jira",
        title="t",
        description="",
        url="",
        priority=TaskPriority.MEDIUM,
    )


@pytest.mark.asyncio
async def test_new_tasks_trigger_publish(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tracker = _FakeTracker([[_task("DM-1"), _task("DM-2")]])
    bus = _SpyBus()
    orch = Orchestrator(
        task_tracker=tracker,
        session_factory=session_factory,
        config=_cfg(),
        message_bus=bus,
    )

    stats = await orch.run_once()
    assert stats.created == 2
    assert stats.dispatched == 2
    assert [m.topic for m in bus.published] == [
        TOPIC_TASK_DISCOVERED,
        TOPIC_TASK_DISCOVERED,
    ]
    assert all(m.to_agent == AGENT_ANALYST for m in bus.published)
    payloads = sorted(m.payload["external_id"] for m in bus.published)
    assert payloads == ["DM-1", "DM-2"]


@pytest.mark.asyncio
async def test_updates_do_not_trigger_publish(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tracker = _FakeTracker([[_task("DM-1")], [_task("DM-1")]])
    bus = _SpyBus()
    orch = Orchestrator(
        task_tracker=tracker,
        session_factory=session_factory,
        config=_cfg(),
        message_bus=bus,
    )

    await orch.run_once()  # creates, publishes 1
    await orch.run_once()  # updates, publishes 0

    assert len(bus.published) == 1


@pytest.mark.asyncio
async def test_no_bus_means_no_publish(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tracker = _FakeTracker([[_task("DM-1")]])
    orch = Orchestrator(
        task_tracker=tracker,
        session_factory=session_factory,
        config=_cfg(),
        message_bus=None,
    )

    stats = await orch.run_once()
    assert stats.created == 1
    assert stats.dispatched == 0
