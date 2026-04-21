"""Orchestrator — polls the tracker and routes new tasks onto the message bus.

Phase 0 kept it as a direct DB writer. Phase 1 adds one responsibility: when
a task is newly discovered (insert), publish a ``task.discovered`` message
so the Analyst can pick it up via the bus. That keeps the architectural rule
"agents talk only through the bus" intact.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.domain.models.task import Task
from virtual_dev.domain.ports.message_bus import AgentMessage, MessageBusPort
from virtual_dev.domain.ports.task_tracker import TaskTrackerPort
from virtual_dev.infrastructure.config import AppConfig
from virtual_dev.infrastructure.db import TaskRow
from virtual_dev.infrastructure.db.base import session_scope
from virtual_dev.infrastructure.db.mappers import task_to_row, update_row_from_task

TOPIC_TASK_DISCOVERED = "task.discovered"
TOPIC_PLAN_READY = "plan.ready"
AGENT_ANALYST = "analyst"


def dev_agent_key(repo_key: str, specialisation: str = "backend") -> str:
    """Conventional agent-key used on the bus for Dev-agent routing."""
    return f"dev-{repo_key}-{specialisation}"


@dataclass
class OrchestratorRunStats:
    """Per-iteration counters, handy for tests and the dashboard."""

    fetched: int = 0
    created: int = 0
    updated: int = 0
    dispatched: int = 0


class Orchestrator:
    """Polls the tracker; upserts tasks; dispatches new ones onto the bus."""

    agent_key = "orchestrator"

    def __init__(
        self,
        *,
        task_tracker: TaskTrackerPort | None,
        session_factory: async_sessionmaker[AsyncSession],
        config: AppConfig,
        message_bus: MessageBusPort | None = None,
    ) -> None:
        self._task_tracker = task_tracker
        self._session_factory = session_factory
        self._config = config
        self._message_bus = message_bus
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

        newly_created: list[Task] = []
        async with session_scope(self._session_factory) as session:
            for task in tasks:
                created = await self._upsert_task(session, task)
                if created:
                    stats.created += 1
                    newly_created.append(task)
                else:
                    stats.updated += 1

        # Publish AFTER commit so subscribers can safely read the task row.
        if self._message_bus is not None:
            for task in newly_created:
                await self._dispatch_discovered(task)
                stats.dispatched += 1

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

    async def _dispatch_discovered(self, task: Task) -> None:
        assert self._message_bus is not None
        await self._message_bus.publish(
            AgentMessage(
                id=uuid.uuid4().hex,
                from_agent=self.agent_key,
                to_agent=AGENT_ANALYST,
                topic=TOPIC_TASK_DISCOVERED,
                payload={"tracker": task.tracker, "external_id": task.external_id},
            )
        )
