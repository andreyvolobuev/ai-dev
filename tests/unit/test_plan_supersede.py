"""save_plan must supersede prior non-SUPERSEDED plans for the same task.

The plans table is append-only by design (audit trail) — each save adds
a row. Without superseding the previous one, the table grows
unbounded over time and every "find the active plan" query has to
``order_by created_at desc limit 1`` instead of using a status filter.

PlanStatus.SUPERSEDED was already part of the enum but nothing wrote
it; queries relied on createdAt ordering alone. Now save_plan flips
prior rows to SUPERSEDED before inserting the new one.
"""

from __future__ import annotations

from typing import cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.domain.models.plan import Plan, PlanStatus
from virtual_dev.infrastructure.db import PlanRow


def _plan(*, status: PlanStatus = PlanStatus.READY, summary: str = "x") -> Plan:
    return Plan(
        tracker="jira", task_external_id="DM-PLAN",
        summary=summary, steps=[], open_questions=[], risks=[],
        confidence=0.9, status=status, target_repo_key="bellingshausen",
        cost_usd=0.0, iterations=1, model="m", agent_key="analyst",
    )


async def _save(repo, plan: Plan) -> None:  # type: ignore[no-untyped-def]
    await repo.save_plan(plan)


@pytest.fixture
def repo(session_factory: async_sessionmaker[AsyncSession]):  # type: ignore[no-untyped-def]
    """Build a minimal AnalystAgent shell — only the persistence helpers
    are exercised, so the heavy dependencies stay None / stub."""
    from virtual_dev.application.agents.analyst import AnalystAgent

    agent = AnalystAgent.__new__(AnalystAgent)
    # Only the session_factory is touched by save_plan / has_fresh_plan.
    agent._session_factory = session_factory  # type: ignore[attr-defined]
    return agent


@pytest.mark.asyncio
async def test_save_plan_supersedes_prior_active_plans(
    session_factory: async_sessionmaker[AsyncSession],
    repo,  # type: ignore[no-untyped-def]
) -> None:
    await _save(repo, _plan(summary="first"))
    await _save(repo, _plan(summary="second"))

    async with session_factory() as session:
        rows = list((await session.execute(
            select(PlanRow)
            .where(PlanRow.task_external_id == "DM-PLAN")
            .order_by(PlanRow.created_at.asc())
        )).scalars().all())

    assert len(rows) == 2  # both kept (audit trail)
    statuses = [r.status for r in rows]
    assert statuses[0] == PlanStatus.SUPERSEDED.value, (
        f"first plan must be SUPERSEDED after second save, got {statuses[0]!r}"
    )
    assert statuses[1] == PlanStatus.READY.value


@pytest.mark.asyncio
async def test_save_plan_does_not_touch_other_tasks_plans(
    session_factory: async_sessionmaker[AsyncSession],
    repo,  # type: ignore[no-untyped-def]
) -> None:
    """Superseding must scope to ``(tracker, task_external_id)`` —
    otherwise saving DM-2's plan flips DM-1's plan to superseded too."""
    other = Plan(
        tracker="jira", task_external_id="DM-OTHER",
        summary="other task", steps=[], open_questions=[], risks=[],
        confidence=0.9, status=PlanStatus.READY, target_repo_key="x",
        cost_usd=0.0, iterations=1, model="m", agent_key="analyst",
    )
    await _save(repo, other)
    await _save(repo, _plan(summary="dm-plan"))

    async with session_factory() as session:
        other_row = (await session.execute(
            select(PlanRow).where(PlanRow.task_external_id == "DM-OTHER")
        )).scalar_one()
    assert cast(str, other_row.status) == PlanStatus.READY.value


@pytest.mark.asyncio
async def test_active_plan_lookup_uses_status_filter(
    session_factory: async_sessionmaker[AsyncSession],
    repo,  # type: ignore[no-untyped-def]
) -> None:
    """After superseding, ``status != SUPERSEDED`` is enough to find
    the active plan — no ``order_by created_at desc limit 1`` race."""
    await _save(repo, _plan(summary="first"))
    await _save(repo, _plan(summary="second"))
    await _save(repo, _plan(summary="third"))

    async with session_factory() as session:
        active = list((await session.execute(
            select(PlanRow).where(
                PlanRow.task_external_id == "DM-PLAN",
                PlanRow.status != PlanStatus.SUPERSEDED.value,
            )
        )).scalars().all())

    assert len(active) == 1
    assert active[0].summary == "third"
