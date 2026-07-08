"""Recovery sweep — re-publish plan.ready for tasks stuck in CODING.

Stage 16 covers the case where dev raises mid-run (lease expires, bus
redelivers). This is the wider safety net: if the bus message was
already ack'd before the failure (process killed between ack and
side effect, FAILED outcome that operator wants retried, message
manually consumed during debugging, etc.), the recovery sweep finds
tasks stuck in CODING with an active plan and no open MR, and
publishes the plan.ready event again.

Dedup is by inflight-message detection: if a plan.ready for the same
task is already on the bus and unconsumed, the sweep skips it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.adapters.message_bus import InMemoryMessageBus
from virtual_dev.application.agents.orchestrator import (
    TOPIC_PLAN_READY,
    TOPIC_TASK_DISCOVERED,
    dev_agent_key,
)
from virtual_dev.application.services.recovery_service import RecoveryService
from virtual_dev.domain.models.merge_request import MRStatus, PipelineStatus
from virtual_dev.domain.models.plan import PlanStatus
from virtual_dev.domain.models.task import TaskStatus
from virtual_dev.infrastructure.db import (
    MergeRequestRow,
    PlanRow,
    TaskRow,
)
from virtual_dev.infrastructure.db.base import session_scope


_OLD = datetime.now(timezone.utc) - timedelta(hours=2)


async def _insert_task(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    external_id: str = "DM-STUCK",
    internal_status: str = TaskStatus.CODING.value,
    updated_at: datetime = _OLD,
) -> int:
    async with session_scope(session_factory) as session:
        row = TaskRow(
            tracker="jira", external_id=external_id,
            title="t", description="", url="",
            components_json=[], labels_json=[], links_json=[],
            priority="medium", external_status="In Progress",
            internal_status=internal_status, dor_satisfied=False,
            target_repo_key="bellingshausen",
            discovered_at=updated_at, updated_at=updated_at,
        )
        session.add(row)
        await session.flush()
        return row.id  # type: ignore[return-value]


async def _insert_plan(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    external_id: str = "DM-STUCK",
    status: PlanStatus = PlanStatus.READY,
) -> None:
    async with session_scope(session_factory) as session:
        session.add(PlanRow(
            tracker="jira", task_external_id=external_id,
            summary="x", steps_json=[], open_questions_json=[], risks_json=[],
            confidence=0.9, status=status.value,
            target_repo_key="bellingshausen",
            cost_usd=0.0, iterations=1, model="m", agent_key="analyst",
        ))


async def _insert_mr(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    external_id: str = "DM-STUCK",
    status: str = MRStatus.OPEN.value,
) -> None:
    async with session_scope(session_factory) as session:
        session.add(MergeRequestRow(
            repo_key="bellingshausen", iid=1,
            external_id="1", task_external_id=external_id,
            title="m", description="", source_branch="x", target_branch="main",
            author_username="bot", web_url="",
            status=status,
            approvals_count=0, approvals_required=1,
            pipeline_status=PipelineStatus.UNKNOWN.value, pipeline_url="",
        ))


@pytest.mark.asyncio
async def test_sweep_republishes_for_stuck_coding_task(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_task(session_factory)
    await _insert_plan(session_factory)
    bus = InMemoryMessageBus()
    # Subscribe so the in-memory bus knows where broadcasts go (here we
    # publish to a specific dev agent, but we still need the queue to
    # exist before publish so the test can drain it).
    await bus.subscribe(dev_agent_key("bellingshausen", "backend"))

    svc = RecoveryService(
        session_factory=session_factory, message_bus=bus,
        stuck_after=timedelta(minutes=30),
    )
    n = await svc.sweep_stuck_tasks()
    assert n == 1

    sub = await bus.subscribe(dev_agent_key("bellingshausen", "backend"))
    msg = await sub.__anext__()
    assert msg.topic == TOPIC_PLAN_READY
    assert msg.payload["external_id"] == "DM-STUCK"
    assert msg.payload["repo_key"] == "bellingshausen"


@pytest.mark.asyncio
async def test_sweep_skips_recently_updated_tasks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Tasks updated within the threshold are still 'in flight' from
    the runner's perspective — don't double-dispatch."""
    fresh = datetime.now(timezone.utc) - timedelta(minutes=5)
    await _insert_task(session_factory, updated_at=fresh)
    await _insert_plan(session_factory)
    bus = InMemoryMessageBus()
    await bus.subscribe(dev_agent_key("bellingshausen", "backend"))

    svc = RecoveryService(
        session_factory=session_factory, message_bus=bus,
        stuck_after=timedelta(minutes=30),
    )
    n = await svc.sweep_stuck_tasks()
    assert n == 0


@pytest.mark.asyncio
async def test_sweep_skips_tasks_with_open_mr(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """If an MR is already open, dev finished its job — no need to
    re-dispatch even if the task row is still in CODING."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory)
    await _insert_mr(session_factory)
    bus = InMemoryMessageBus()
    await bus.subscribe(dev_agent_key("bellingshausen", "backend"))

    svc = RecoveryService(
        session_factory=session_factory, message_bus=bus,
        stuck_after=timedelta(minutes=30),
    )
    n = await svc.sweep_stuck_tasks()
    assert n == 0


@pytest.mark.asyncio
async def test_sweep_skips_tasks_without_active_plan(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A SUPERSEDED plan means the analyst is reworking; don't dispatch
    against it. No plan at all → analyst hasn't finished; same."""
    await _insert_task(session_factory)
    await _insert_plan(session_factory, status=PlanStatus.SUPERSEDED)
    bus = InMemoryMessageBus()
    await bus.subscribe(dev_agent_key("bellingshausen", "backend"))

    svc = RecoveryService(
        session_factory=session_factory, message_bus=bus,
        stuck_after=timedelta(minutes=30),
    )
    n = await svc.sweep_stuck_tasks()
    assert n == 0


@pytest.mark.asyncio
async def test_sweep_skips_tasks_in_other_statuses(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Only CODING is auto-recoverable. DISCOVERED / PLANNING / READY
    (analyst's domain) and MR_OPEN (dev finished) are out of scope."""
    await _insert_task(
        session_factory, external_id="DM-PLANNING",
        internal_status=TaskStatus.PLANNING.value,
    )
    await _insert_plan(session_factory, external_id="DM-PLANNING")
    bus = InMemoryMessageBus()
    await bus.subscribe(dev_agent_key("bellingshausen", "backend"))

    svc = RecoveryService(
        session_factory=session_factory, message_bus=bus,
        stuck_after=timedelta(minutes=30),
    )
    n = await svc.sweep_stuck_tasks()
    assert n == 0


@pytest.mark.asyncio
async def test_sweep_republishes_task_discovered_for_orphaned_tasks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A task stranded in DISCOVERED (dispatch lost between the
    orchestrator's commit and publish) is re-dispatched to the analyst."""
    await _insert_task(
        session_factory, external_id="DM-ORPHAN",
        internal_status=TaskStatus.DISCOVERED.value,
    )
    bus = InMemoryMessageBus()
    await bus.subscribe("analyst")

    svc = RecoveryService(
        session_factory=session_factory, message_bus=bus,
        stuck_after=timedelta(minutes=30),
    )
    n = await svc.sweep_undispatched_tasks()
    assert n == 1

    sub = await bus.subscribe("analyst")
    msg = await sub.__anext__()
    assert msg.topic == TOPIC_TASK_DISCOVERED
    assert msg.payload["external_id"] == "DM-ORPHAN"


@pytest.mark.asyncio
async def test_undispatched_sweep_skips_recent_and_non_discovered(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_task(
        session_factory, external_id="DM-FRESH",
        internal_status=TaskStatus.DISCOVERED.value,
        updated_at=datetime.now(timezone.utc),
    )
    await _insert_task(
        session_factory, external_id="DM-PLANNING",
        internal_status=TaskStatus.PLANNING.value,
    )
    bus = InMemoryMessageBus()
    await bus.subscribe("analyst")

    svc = RecoveryService(
        session_factory=session_factory, message_bus=bus,
        stuck_after=timedelta(minutes=30),
    )
    assert await svc.sweep_undispatched_tasks() == 0
