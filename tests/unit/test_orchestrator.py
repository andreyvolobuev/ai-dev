"""Orchestrator unit tests with a fake task tracker."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents import Orchestrator
from virtual_dev.domain.models.task import Task, TaskPriority, TaskStatus
from virtual_dev.domain.ports.task_tracker import TaskTrackerPort
from virtual_dev.infrastructure.config.schema import (
    AgentsCfg,
    AppConfig,
    MappingsCfg,
    TaskSourceCfg,
)
from virtual_dev.infrastructure.db import TaskRow


class _FakeTracker(TaskTrackerPort):
    def __init__(self, tasks: list[Task]) -> None:
        self.tasks = tasks
        self.calls = 0

    async def fetch_tasks(self, jql: str, limit: int = 50) -> Sequence[Task]:
        self.calls += 1
        return list(self.tasks)

    async def get_task(self, external_id: str) -> Task:
        raise NotImplementedError

    async def transition(self, external_id: str, to_status: str) -> None:
        raise NotImplementedError

    async def comment(self, external_id: str, body: str) -> None:
        raise NotImplementedError


def _cfg(poll: int = 120, jql: str = "project = X") -> AppConfig:
    return AppConfig(
        repositories=[],
        agents=AgentsCfg(task_source=TaskSourceCfg(jql=jql, poll_interval_seconds=poll)),
        mappings=MappingsCfg(),
    )


def _task(external_id: str, title: str = "t") -> Task:
    return Task(
        external_id=external_id,
        tracker="jira",
        title=title,
        description="",
        url=f"https://jira/{external_id}",
        priority=TaskPriority.MEDIUM,
        external_status="To Do",
        internal_status=TaskStatus.DISCOVERED,
    )


@pytest.mark.asyncio
async def test_orchestrator_upserts_tasks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tracker = _FakeTracker([_task("DM-1"), _task("DM-2")])
    orch = Orchestrator(task_tracker=tracker, session_factory=session_factory, config=_cfg())

    stats = await orch.run_once()
    assert stats.fetched == 2
    assert stats.created == 2
    assert stats.updated == 0

    # Second call: same tasks → updates, not inserts.
    stats = await orch.run_once()
    assert stats.created == 0
    assert stats.updated == 2

    async with session_factory() as session:
        rows = (await session.execute(select(TaskRow))).scalars().all()
    assert {row.external_id for row in rows} == {"DM-1", "DM-2"}


@pytest.mark.asyncio
async def test_orchestrator_noop_without_tracker(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    orch = Orchestrator(task_tracker=None, session_factory=session_factory, config=_cfg())
    stats = await orch.run_once()
    assert stats.fetched == 0
    assert stats.created == 0
