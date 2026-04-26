"""Tests for GoalRepository — the SQL-owning layer of the goal-driven
clarification subsystem (Phase 3.9, replaces QuestionRepository).

Covers create/read, idle-coalescer/deadline/wait queries, claim_for_replan
atomicity, fragment idempotency (UNIQUE per goal_id+mm_post_id), step seq
monotonicity, archive_unflushed_as_stale, race-safe deadline check.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.services.clarification.goal_repo import GoalRepository
from virtual_dev.domain.models.clarification_goal import (
    GoalState,
    GoalStepKind,
)
from virtual_dev.infrastructure.db import GoalRow
from virtual_dev.infrastructure.db.base import session_scope

# ============================================================
# Helpers
# ============================================================


def _deadline() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=48)


async def _seed(
    repo: GoalRepository,
    *,
    plan_id: int = 1,
    description: str = "узнать body запроса",
    coalesce_window_seconds: int = 600,
):
    return await repo.create_goal(
        plan_id=plan_id,
        tracker="jira",
        task_external_id="DM-1",
        description=description,
        why_it_matters="нужно для воспроизведения",
        initial_contact_hint="alice",
        coalesce_window_seconds=coalesce_window_seconds,
        deadline_at=_deadline(),
    )


# ============================================================
# create / read
# ============================================================


@pytest.mark.asyncio
async def test_create_goal_starts_in_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = GoalRepository(session_factory)
    goal = await _seed(repo)
    assert goal.state == GoalState.PENDING
    assert goal.planner_calls_count == 0
    assert goal.send_retry_count == 0
    assert goal.deadline_at is not None
    fetched = await repo.get(goal.id)
    assert fetched is not None
    assert fetched.description == "узнать body запроса"


@pytest.mark.asyncio
async def test_list_for_plan_returns_in_id_order(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = GoalRepository(session_factory)
    a = await _seed(repo, description="A")
    b = await _seed(repo, description="B")
    c = await _seed(repo, plan_id=2, description="C")
    plan_one = await repo.list_for_plan(1)
    assert [g.id for g in plan_one] == [a.id, b.id]
    plan_two = await repo.list_for_plan(2)
    assert [g.id for g in plan_two] == [c.id]


@pytest.mark.asyncio
async def test_list_for_task_groups_by_tracker_and_external_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = GoalRepository(session_factory)
    g1 = await repo.create_goal(
        plan_id=1, tracker="jira", task_external_id="DM-1",
        description="x", why_it_matters="", initial_contact_hint="",
        coalesce_window_seconds=600, deadline_at=_deadline(),
    )
    g2 = await repo.create_goal(
        plan_id=1, tracker="jira", task_external_id="DM-2",
        description="y", why_it_matters="", initial_contact_hint="",
        coalesce_window_seconds=600, deadline_at=_deadline(),
    )
    one = await repo.list_for_task("jira", "DM-1")
    assert [g.id for g in one] == [g1.id]
    two = await repo.list_for_task("jira", "DM-2")
    assert [g.id for g in two] == [g2.id]


@pytest.mark.asyncio
async def test_existing_descriptions_excludes_terminal_goals(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Abandoned goals must not block re-asking the same question."""
    repo = GoalRepository(session_factory)
    await _seed(repo, description="active-q")
    abandoned = await _seed(repo, description="abandoned-q")
    await repo.update_state(abandoned.id, GoalState.ABANDONED, closed=True)
    descriptions = await repo.existing_descriptions_for_plan(1)
    assert "active-q" in descriptions
    assert "abandoned-q" not in descriptions


# ============================================================
# find_active_by_thread / find_active_by_channel
# ============================================================


@pytest.mark.asyncio
async def test_find_active_by_thread_only_returns_active(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = GoalRepository(session_factory)
    goal = await _seed(repo)
    await repo.update_state(
        goal.id, GoalState.AWAITING_REPLY,
        outstanding_post_id="post-A",
        outstanding_user_id="uid-1",
        outstanding_username="alice",
        outstanding_channel="dm-1",
    )
    found = await repo.find_active_by_thread("post-A")
    assert found is not None
    assert found.id == goal.id

    # Goal closed — find should return None.
    await repo.update_state(goal.id, GoalState.ACHIEVED, closed=True)
    assert await repo.find_active_by_thread("post-A") is None


@pytest.mark.asyncio
async def test_find_active_by_channel_picks_most_recent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two active goals in same DM channel — newest asked_at wins."""
    repo = GoalRepository(session_factory)
    a = await _seed(repo, description="old-q")
    b = await _seed(repo, description="new-q")
    await repo.update_state(
        a.id, GoalState.AWAITING_REPLY,
        outstanding_post_id="post-A",
        outstanding_user_id="uid-x", outstanding_username="x",
        outstanding_channel="dm-x",
    )
    await repo.update_state(
        b.id, GoalState.AWAITING_REPLY,
        outstanding_post_id="post-B",
        outstanding_user_id="uid-x", outstanding_username="x",
        outstanding_channel="dm-x",
    )
    older_t = datetime.now(timezone.utc) - timedelta(hours=2)
    newer_t = datetime.now(timezone.utc) - timedelta(hours=1)
    async with session_scope(session_factory) as session:
        rows = list((await session.execute(select(GoalRow))).scalars())
        for r in rows:
            r.asked_at = older_t if r.id == a.id else newer_t
    chosen = await repo.find_active_by_channel("dm-x", "uid-x")
    assert chosen is not None
    assert chosen.id == b.id, "find_active_by_channel must pick newest goal"


# ============================================================
# claim_for_replan — atomic flip
# ============================================================


@pytest.mark.asyncio
async def test_claim_for_replan_flips_coalescing_to_replanning(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = GoalRepository(session_factory)
    goal = await _seed(repo)
    await repo.update_state(goal.id, GoalState.COALESCING)
    claimed = await repo.claim_for_replan(goal.id)
    assert claimed is not None
    assert claimed.state == GoalState.REPLANNING
    # Second claim returns None (idempotent — only one winner).
    again = await repo.claim_for_replan(goal.id)
    assert again is None


@pytest.mark.asyncio
async def test_claim_for_replan_rejects_terminal_state(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = GoalRepository(session_factory)
    goal = await _seed(repo)
    await repo.update_state(goal.id, GoalState.ACHIEVED, closed=True)
    assert await repo.claim_for_replan(goal.id) is None


@pytest.mark.asyncio
async def test_claim_for_replan_records_started_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The crash-recovery sweep relies on last_planning_started_at."""
    repo = GoalRepository(session_factory)
    goal = await _seed(repo)
    await repo.update_state(goal.id, GoalState.READY_TO_REPLAN)
    await repo.claim_for_replan(goal.id)
    async with session_scope(session_factory) as session:
        row = (await session.execute(
            select(GoalRow).where(GoalRow.id == goal.id)
        )).scalar_one()
        assert row.last_planning_started_at is not None


# ============================================================
# coalescer queries
# ============================================================


@pytest.mark.asyncio
async def test_find_idle_coalescing_returns_only_elapsed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = GoalRepository(session_factory)
    elapsed = await _seed(repo, coalesce_window_seconds=60, description="elapsed")
    fresh = await _seed(repo, coalesce_window_seconds=600, description="fresh")
    now = datetime.now(timezone.utc)
    await repo.update_state(
        elapsed.id, GoalState.COALESCING,
        last_fragment_at=now - timedelta(seconds=120),
    )
    await repo.update_state(
        fresh.id, GoalState.COALESCING,
        last_fragment_at=now - timedelta(seconds=10),
    )
    idle = await repo.find_idle_coalescing(now=now)
    ids = {g.id for g in idle}
    assert elapsed.id in ids
    assert fresh.id not in ids


@pytest.mark.asyncio
async def test_find_overdue_returns_active_past_deadline(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = GoalRepository(session_factory)
    overdue = await repo.create_goal(
        plan_id=1, tracker="jira", task_external_id="DM-1",
        description="old", why_it_matters="", initial_contact_hint="",
        coalesce_window_seconds=600,
        deadline_at=datetime.now(timezone.utc) - timedelta(seconds=30),
    )
    fresh = await repo.create_goal(
        plan_id=1, tracker="jira", task_external_id="DM-2",
        description="new", why_it_matters="", initial_contact_hint="",
        coalesce_window_seconds=600,
        deadline_at=datetime.now(timezone.utc) + timedelta(hours=2),
    )
    now = datetime.now(timezone.utc)
    found = await repo.find_overdue(now=now)
    ids = {g.id for g in found}
    assert overdue.id in ids
    assert fresh.id not in ids


@pytest.mark.asyncio
async def test_find_overdue_skips_terminal_states(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = GoalRepository(session_factory)
    g = await repo.create_goal(
        plan_id=1, tracker="jira", task_external_id="X",
        description="x", why_it_matters="", initial_contact_hint="",
        coalesce_window_seconds=600,
        deadline_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    await repo.update_state(g.id, GoalState.ACHIEVED, closed=True)
    found = await repo.find_overdue(now=datetime.now(timezone.utc))
    assert all(x.id != g.id for x in found)


@pytest.mark.asyncio
async def test_find_due_waiting_returns_only_elapsed_next_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = GoalRepository(session_factory)
    due = await _seed(repo, description="due")
    not_due = await _seed(repo, description="not-due")
    now = datetime.now(timezone.utc)
    await repo.update_state(
        due.id, GoalState.WAITING,
        next_planner_run_at=now - timedelta(seconds=10),
    )
    await repo.update_state(
        not_due.id, GoalState.WAITING,
        next_planner_run_at=now + timedelta(minutes=10),
    )
    found = await repo.find_due_waiting(now=now)
    ids = {g.id for g in found}
    assert due.id in ids
    assert not_due.id not in ids


@pytest.mark.asyncio
async def test_find_stuck_replanning_uses_started_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = GoalRepository(session_factory)
    stuck = await _seed(repo, description="stuck")
    fresh = await _seed(repo, description="fresh")
    await repo.update_state(stuck.id, GoalState.COALESCING)
    await repo.claim_for_replan(stuck.id)
    await repo.update_state(fresh.id, GoalState.COALESCING)
    await repo.claim_for_replan(fresh.id)

    # Manually backdate stuck's last_planning_started_at to 30 minutes ago.
    async with session_scope(session_factory) as session:
        row = (await session.execute(
            select(GoalRow).where(GoalRow.id == stuck.id)
        )).scalar_one()
        row.last_planning_started_at = datetime.now(timezone.utc) - timedelta(minutes=30)

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
    found = await repo.find_stuck_replanning(older_than=cutoff)
    ids = {g.id for g in found}
    assert stuck.id in ids
    assert fresh.id not in ids


@pytest.mark.asyncio
async def test_find_pending_send_returns_only_send_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = GoalRepository(session_factory)
    a = await _seed(repo, description="A")
    b = await _seed(repo, description="B")
    await repo.update_state(a.id, GoalState.SEND_PENDING)
    await repo.update_state(b.id, GoalState.AWAITING_REPLY)
    found = await repo.find_pending_send()
    assert {g.id for g in found} == {a.id}


# ============================================================
# fragments — UNIQUE(goal_id, mm_post_id)
# ============================================================


@pytest.mark.asyncio
async def test_append_fragment_dedupes_by_post_id_per_goal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = GoalRepository(session_factory)
    goal = await _seed(repo)
    await repo.update_state(goal.id, GoalState.AWAITING_REPLY)
    now = datetime.now(timezone.utc)
    first = await repo.append_fragment(
        goal_id=goal.id, mm_post_id="p-1",
        asked_post_id="bot-q", text="hi", received_at=now,
    )
    second = await repo.append_fragment(
        goal_id=goal.id, mm_post_id="p-1",
        asked_post_id="bot-q", text="hi-again", received_at=now,
    )
    assert first is True
    assert second is False, "duplicate (goal_id, mm_post_id) must be rejected"
    pending = await repo.list_unflushed_fragments(goal.id)
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_append_fragment_allows_same_post_under_different_goal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two goals in the same channel can each see the same post."""
    repo = GoalRepository(session_factory)
    a = await _seed(repo, description="A")
    b = await _seed(repo, description="B")
    now = datetime.now(timezone.utc)
    ok_a = await repo.append_fragment(
        goal_id=a.id, mm_post_id="p-shared",
        asked_post_id="bot-A", text="x", received_at=now,
    )
    ok_b = await repo.append_fragment(
        goal_id=b.id, mm_post_id="p-shared",
        asked_post_id="bot-B", text="x", received_at=now,
    )
    assert ok_a is True
    assert ok_b is True


@pytest.mark.asyncio
async def test_append_fragment_flips_awaiting_to_coalescing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = GoalRepository(session_factory)
    goal = await _seed(repo)
    await repo.update_state(goal.id, GoalState.AWAITING_REPLY)
    await repo.append_fragment(
        goal_id=goal.id, mm_post_id="p-1",
        asked_post_id=None, text="ответ", received_at=datetime.now(timezone.utc),
    )
    fetched = await repo.get(goal.id)
    assert fetched is not None
    assert fetched.state == GoalState.COALESCING
    assert fetched.last_fragment_at is not None


@pytest.mark.asyncio
async def test_append_fragment_does_not_flip_replanning(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A fragment arriving mid-replan must NOT clobber REPLANNING state."""
    repo = GoalRepository(session_factory)
    goal = await _seed(repo)
    await repo.update_state(goal.id, GoalState.REPLANNING)
    await repo.append_fragment(
        goal_id=goal.id, mm_post_id="p-late",
        asked_post_id=None, text="late",
        received_at=datetime.now(timezone.utc),
    )
    fetched = await repo.get(goal.id)
    assert fetched is not None
    assert fetched.state == GoalState.REPLANNING


@pytest.mark.asyncio
async def test_archive_unflushed_as_stale_moves_to_steps(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When a new ASK supersedes the previous question, pending fragments
    are archived as STALE_FRAGMENT steps so the audit trail keeps them
    but they don't appear in the next planner-call's evidence."""
    repo = GoalRepository(session_factory)
    goal = await _seed(repo)
    await repo.update_state(goal.id, GoalState.AWAITING_REPLY)
    now = datetime.now(timezone.utc)
    await repo.append_fragment(
        goal_id=goal.id, mm_post_id="p-1",
        asked_post_id=None, text="frag-1", received_at=now,
    )
    await repo.append_fragment(
        goal_id=goal.id, mm_post_id="p-2",
        asked_post_id=None, text="frag-2", received_at=now + timedelta(seconds=1),
    )
    archived = await repo.archive_unflushed_as_stale(goal.id)
    assert archived == 2
    # Now buffer is empty.
    assert await repo.list_unflushed_fragments(goal.id) == []
    # Steps contain stale-fragment kind.
    steps = await repo.list_steps(goal.id)
    kinds = [s.kind for s in steps]
    assert kinds == [GoalStepKind.STALE_FRAGMENT, GoalStepKind.STALE_FRAGMENT]
    assert [s.text for s in steps] == ["frag-1", "frag-2"]
    # The mm_post_id is preserved in metadata for traceability.
    assert steps[0].metadata.get("mm_post_id") == "p-1"
    assert steps[1].metadata.get("mm_post_id") == "p-2"


# ============================================================
# steps — append-only, monotonic seq
# ============================================================


@pytest.mark.asyncio
async def test_append_step_seq_is_monotonic(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = GoalRepository(session_factory)
    goal = await _seed(repo)
    s1 = await repo.append_step(
        goal_id=goal.id, kind=GoalStepKind.BOT_ASKED,
        text="q1", target_username="alice",
    )
    s2 = await repo.append_step(
        goal_id=goal.id, kind=GoalStepKind.HUMAN_REPLIED, text="ans",
    )
    s3 = await repo.append_step(
        goal_id=goal.id, kind=GoalStepKind.PLANNER_DECIDED, text="next-step",
    )
    assert [s.seq for s in (s1, s2, s3)] == [1, 2, 3]
    listed = await repo.list_steps(goal.id)
    assert [s.seq for s in listed] == [1, 2, 3]
    assert [s.kind for s in listed] == [
        GoalStepKind.BOT_ASKED,
        GoalStepKind.HUMAN_REPLIED,
        GoalStepKind.PLANNER_DECIDED,
    ]


@pytest.mark.asyncio
async def test_list_steps_returns_in_seq_order(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = GoalRepository(session_factory)
    goal = await _seed(repo)
    for i in range(5):
        await repo.append_step(
            goal_id=goal.id, kind=GoalStepKind.NOTE, text=f"note-{i}",
        )
    listed = await repo.list_steps(goal.id)
    assert [s.text for s in listed] == [f"note-{i}" for i in range(5)]


# ============================================================
# state mutations
# ============================================================


@pytest.mark.asyncio
async def test_update_state_clear_outstanding_resets_target_fields(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = GoalRepository(session_factory)
    goal = await _seed(repo)
    await repo.update_state(
        goal.id, GoalState.AWAITING_REPLY,
        outstanding_post_id="p", outstanding_user_id="u",
        outstanding_username="n", outstanding_channel="c",
        outstanding_text="text",
        last_fragment_at=datetime.now(timezone.utc),
    )
    fetched = await repo.get(goal.id)
    assert fetched is not None and fetched.current_target_user_id == "u"
    await repo.update_state(
        goal.id, GoalState.ACHIEVED, clear_outstanding=True, closed=True,
    )
    fetched = await repo.get(goal.id)
    assert fetched is not None
    assert fetched.current_target_user_id is None
    assert fetched.current_asked_post_id is None
    assert fetched.last_fragment_at is None


@pytest.mark.asyncio
async def test_update_state_increments_planner_calls(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = GoalRepository(session_factory)
    goal = await _seed(repo)
    for _ in range(3):
        await repo.update_state(
            goal.id, GoalState.PLANNING, increment_planner_calls=True,
        )
    fetched = await repo.get(goal.id)
    assert fetched is not None and fetched.planner_calls_count == 3


@pytest.mark.asyncio
async def test_set_outstanding_clears_send_retry_count(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = GoalRepository(session_factory)
    goal = await _seed(repo)
    await repo.update_state(
        goal.id, GoalState.SEND_PENDING, increment_send_retry=True,
    )
    fetched = await repo.get(goal.id)
    assert fetched is not None and fetched.send_retry_count == 1
    await repo.set_outstanding(
        goal.id, target_user_id="u", target_username="n",
        channel_id="c", asked_post_id="p", asked_text="hi", dedupe_key=None,
    )
    fetched = await repo.get(goal.id)
    assert fetched is not None and fetched.send_retry_count == 0
    assert fetched.state == GoalState.AWAITING_REPLY


# ============================================================
# race-safe deadline check
# ============================================================


@pytest.mark.asyncio
async def test_race_safe_check_no_new_fragment_returns_false_after_late_reply(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Reply landed AFTER the sweep selected; check must return False so
    the sweep skips abandoning this goal."""
    repo = GoalRepository(session_factory)
    goal = await _seed(repo)
    await repo.update_state(goal.id, GoalState.AWAITING_REPLY)
    sweep_started = datetime.now(timezone.utc)
    # Fragment lands AFTER sweep_started.
    await asyncio.sleep(0.01)
    await repo.append_fragment(
        goal_id=goal.id, mm_post_id="p-late",
        asked_post_id=None, text="reply",
        received_at=datetime.now(timezone.utc),
    )
    is_clean = await repo.race_safe_check_no_new_fragment(
        goal.id, since=sweep_started,
    )
    assert is_clean is False


@pytest.mark.asyncio
async def test_race_safe_check_returns_true_when_quiet(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = GoalRepository(session_factory)
    goal = await _seed(repo)
    await repo.update_state(goal.id, GoalState.AWAITING_REPLY)
    is_clean = await repo.race_safe_check_no_new_fragment(
        goal.id, since=datetime.now(timezone.utc),
    )
    assert is_clean is True
