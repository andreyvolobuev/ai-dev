"""Periodic safety net for tasks that got stuck in CODING.

Most failure modes are already covered by the message bus's
lease/redelivery (Stage 10) plus dev raising on infra failures
(Stage 16). What's NOT covered:

* The bus message was acked before the underlying side effect
  completed (process killed in the gap, manual ack during debug).
* Dev returned ``DevOutcome.FAILED`` cleanly because the model gave
  up — operator wants to retry without manually flipping rows.

Sweep strategy: for every TaskRow stuck in CODING longer than
``stuck_after`` with an active READY plan and no open MR, re-publish
``plan.ready`` to the appropriate dev agent. The dev agent's
``_precheck`` (ALREADY_HAS_MR) guards against double-dispatch in
case the recovery sweep races a real plan.ready.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents.orchestrator import (
    TOPIC_PLAN_READY,
    dev_agent_key,
)
from virtual_dev.domain.models.merge_request import MRStatus
from virtual_dev.domain.models.plan import PlanStatus
from virtual_dev.domain.models.task import TaskStatus
from virtual_dev.domain.ports.message_bus import AgentMessage, MessageBusPort
from virtual_dev.infrastructure.db import (
    MergeRequestRow,
    PlanRow,
    TaskRow,
)


_DEFAULT_STUCK_AFTER = timedelta(minutes=30)


class RecoveryService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        message_bus: MessageBusPort,
        stuck_after: timedelta = _DEFAULT_STUCK_AFTER,
        dev_specialisation: str = "backend",
    ) -> None:
        self._session_factory = session_factory
        self._bus = message_bus
        self._stuck_after = stuck_after
        self._dev_specialisation = dev_specialisation

    async def sweep_stuck_tasks(self) -> int:
        """Re-publish plan.ready for stuck CODING tasks. Returns the
        number of messages emitted."""
        cutoff = _now() - self._stuck_after
        async with self._session_factory() as session:
            stuck = await self._find_stuck(session, cutoff)
        published = 0
        for row in stuck:
            try:
                if await self._republish(row):
                    published += 1
            except Exception:
                logger.exception(
                    "RecoveryService: failed to republish plan.ready for {}",
                    row.external_id,
                )
        if published:
            logger.info(
                "RecoveryService: re-dispatched {} stuck task(s)", published,
            )
        return published

    async def _find_stuck(
        self,
        session: AsyncSession,
        cutoff: datetime,
    ) -> list[TaskRow]:
        # Naive cutoff stored as naive UTC matches how SQLite reads
        # back the column (see SqliteMessageBus._naive() for the same
        # reasoning).
        cutoff_naive = cutoff.replace(tzinfo=None) if cutoff.tzinfo else cutoff
        stmt = (
            select(TaskRow)
            .where(
                TaskRow.internal_status == TaskStatus.CODING.value,
                TaskRow.updated_at <= cutoff_naive,
            )
        )
        return list((await session.execute(stmt)).scalars().all())

    async def _republish(self, task: TaskRow) -> bool:
        async with self._session_factory() as session:
            plan = await self._active_plan(session, task)
            if plan is None:
                return False
            if await self._has_open_mr(session, task.external_id):
                return False
        target_repo_key = plan.target_repo_key or task.target_repo_key
        if not target_repo_key:
            return False
        await self._bus.publish(AgentMessage(
            id=uuid.uuid4().hex,
            from_agent="recovery-sweep",
            to_agent=dev_agent_key(target_repo_key, self._dev_specialisation),
            topic=TOPIC_PLAN_READY,
            payload={
                "tracker": task.tracker,
                "external_id": task.external_id,
                "repo_key": target_repo_key,
            },
            correlation_id=f"recovery:{task.tracker}:{task.external_id}",
        ))
        logger.warning(
            "RecoveryService: republished plan.ready for {} (stuck in CODING)",
            task.external_id,
        )
        return True

    async def _active_plan(
        self,
        session: AsyncSession,
        task: TaskRow,
    ) -> PlanRow | None:
        stmt = (
            select(PlanRow)
            .where(
                PlanRow.tracker == task.tracker,
                PlanRow.task_external_id == task.external_id,
                PlanRow.status == PlanStatus.READY.value,
            )
            .order_by(PlanRow.created_at.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _has_open_mr(
        self,
        session: AsyncSession,
        external_id: str,
    ) -> bool:
        stmt = (
            select(MergeRequestRow)
            .where(
                MergeRequestRow.task_external_id == external_id,
                MergeRequestRow.status.in_(
                    [MRStatus.OPEN.value, MRStatus.DRAFT.value]
                ),
            )
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none() is not None


def _now() -> datetime:
    return datetime.now(timezone.utc)
