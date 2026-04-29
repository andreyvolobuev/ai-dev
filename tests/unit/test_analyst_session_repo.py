"""``append_step`` race-resilience tests.

The conversation history seq column is UNIQUE per task, and the
default implementation computed ``next_seq = MAX(seq)+1`` then INSERT
without locking. Two append_step calls in the same wall-clock window
(one from the analyst's own ``_record_run`` and one from a side-effect
``_apply_effects.ASK``) raced and one of them lost the IntegrityError
race — its step was rolled back and the conversation log got a hole.

The contract: concurrent append_step calls must both succeed with
distinct ``seq`` values, retrying on IntegrityError up to a small cap.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.services.analyst_session_repo import (
    AnalystSessionRepository,
    ConversationStepKind,
)
from virtual_dev.infrastructure.db import TaskRow
from virtual_dev.infrastructure.db.base import session_scope


async def _make_task(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    async with session_scope(session_factory) as session:
        row = TaskRow(
            tracker="jira", external_id="DM-RACE",
            title="t", description="", url="",
            components_json=[], labels_json=[], links_json=[],
            priority="medium", external_status="In Progress",
            internal_status="ready", dor_satisfied=False,
            target_repo_key=None,
        )
        session.add(row)
        await session.flush()
        return row.id  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_concurrent_append_step_yields_distinct_seqs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two parallel append_step calls in the same task must both land
    with distinct seq values — no IntegrityError leaks out."""
    task_id = await _make_task(session_factory)
    repo = AnalystSessionRepository(session_factory)

    a, b, c = await asyncio.gather(
        repo.append_step(
            task_id=task_id, kind=ConversationStepKind.PLANNER_DECIDED,
            text="A", metadata={},
        ),
        repo.append_step(
            task_id=task_id, kind=ConversationStepKind.PLANNER_DECIDED,
            text="B", metadata={},
        ),
        repo.append_step(
            task_id=task_id, kind=ConversationStepKind.PLANNER_DECIDED,
            text="C", metadata={},
        ),
    )

    seqs = sorted([a.seq, b.seq, c.seq])
    assert seqs == [1, 2, 3], (
        f"expected distinct seqs 1..3, got {seqs}"
    )

    # And the rows themselves must all exist on read-back.
    persisted = await repo.list_steps(task_id)
    assert {s.text for s in persisted} == {"A", "B", "C"}


@pytest.mark.asyncio
async def test_append_step_serial_still_increments(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Sanity: the retry path must not regress the no-contention case."""
    task_id = await _make_task(session_factory)
    repo = AnalystSessionRepository(session_factory)

    s1 = await repo.append_step(
        task_id=task_id, kind=ConversationStepKind.PLANNER_DECIDED,
        text="one", metadata={},
    )
    s2 = await repo.append_step(
        task_id=task_id, kind=ConversationStepKind.HUMAN_REPLIED,
        text="two", metadata={},
    )
    assert s1.seq == 1
    assert s2.seq == 2
