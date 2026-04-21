"""Minimal Orchestrator for Phase 0.

Responsibilities:
    - Poll the task tracker on an interval.
    - Upsert tasks into the DB.
    - Do NOT write to Jira, NOT write to chat, NOT touch code.

Everything else is for later phases.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.domain.models.task import Task
from virtual_dev.domain.ports.task_tracker import TaskTrackerPort
from virtual_dev.infrastructure.config import AppConfig
from virtual_dev.infrastructure.db import TaskRow
from virtual_dev.infrastructure.db.base import session_scope
from virtual_dev.infrastructure.db.mappers import task_to_row, update_row_from_task


@dataclass
class OrchestratorRunStats:
    """Per-iteration counters, handy for tests and the dashboard."""

    fetched: int = 0
    created: int = 0
    updated: int = 0


class Orchestrator:
    """Phase-0 orchestrator: polls Jira, writes tasks to the DB."""

    def __init__(
        self,
        *,
        task_tracker: TaskTrackerPort | None,
        session_factory: async_sessionmaker[AsyncSession],
        config: AppConfig,
    ) -> None:
        self._task_tracker = task_tracker
        self._session_factory = session_factory
        self._config = config
        self._running = False
        self._stop_event = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._running

    async def run_forever(self) -> None:
        """Poll loop. Cancellable via :meth:`stop`."""
        if self._running:
            raise RuntimeError("Orchestrator is already running")
        self._running = True
        self._stop_event.clear()
        interval = self._config.agents.task_source.poll_interval_seconds
        logger.info("Orchestrator started, poll interval = {}s", interval)
        try:
            while not self._stop_event.is_set():
                try:
                    await self.run_once()
                except Exception:  # fail loud, keep looping
                    logger.exception("Orchestrator iteration failed")
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    continue
        finally:
            self._running = False
            logger.info("Orchestrator stopped")

    async def stop(self) -> None:
        self._stop_event.set()

    async def run_once(self) -> OrchestratorRunStats:
        """Single poll iteration. Returns counters."""
        stats = OrchestratorRunStats()

        if self._task_tracker is None:
            logger.debug("Orchestrator tick skipped — no task tracker configured")
            return stats

        jql = self._config.agents.task_source.jql
        tasks = await self._task_tracker.fetch_tasks(jql)
        stats.fetched = len(tasks)
        logger.info("Fetched {} tasks via JQL", stats.fetched)

        async with session_scope(self._session_factory) as session:
            for task in tasks:
                created = await self._upsert_task(session, task)
                if created:
                    stats.created += 1
                else:
                    stats.updated += 1

        return stats

    async def _upsert_task(self, session: AsyncSession, task: Task) -> bool:
        """Insert or update a task row. Returns ``True`` if inserted."""
        stmt = select(TaskRow).where(
            TaskRow.tracker == task.tracker,
            TaskRow.external_id == task.external_id,
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()

        if existing is None:
            session.add(task_to_row(task))
            logger.debug("New task {}: {!r}", task.external_id, task.title)
            return True

        update_row_from_task(existing, task)
        return False
