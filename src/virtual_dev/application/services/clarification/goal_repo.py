"""Repository for the goal-driven clarification subsystem.

Single owner of all SQL against ``clarification_goals``,
``goal_steps`` and ``goal_fragments``. Keeps the
:class:`GoalOrchestrator` body free of ORM details and lets the
state-flip atomicity stay in one place.

Phase 3.9 — replaces :mod:`virtual_dev.application.services.
clarification.repo`. The Q-tree has different invariants (parent
chains, cycle detection, depth limit) — the goal model has none of
those: a goal is one node + an append-only step history.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.domain.models.clarification_goal import (
    ACTIVE_STATES,
    ClarificationGoal,
    GoalState,
    GoalStep,
    GoalStepKind,
)
from virtual_dev.infrastructure.db import (
    GoalFragmentRow,
    GoalRow,
    GoalStepRow,
)
from virtual_dev.infrastructure.db.base import session_scope

# --- Conversion ---


def _row_to_goal(row: GoalRow) -> ClarificationGoal:
    try:
        state = GoalState(row.state)
    except ValueError:
        # Forward-incompatible row (some future code wrote it). Treat
        # as ABANDONED so the orchestrator stops touching it.
        state = GoalState.ABANDONED
    return ClarificationGoal(
        id=row.id,
        plan_id=row.plan_id,
        tracker=row.tracker,
        task_external_id=row.task_external_id,
        description=row.description,
        why_it_matters=row.why_it_matters or "",
        initial_contact_hint=row.initial_contact_hint or "",
        state=state,
        final_answer=row.final_answer,
        parent_goal_id=row.parent_goal_id,
        depth=row.depth or 0,
        current_target_user_id=row.current_target_user_id,
        current_target_username=row.current_target_username,
        current_channel_id=row.current_channel_id,
        current_asked_post_id=row.current_asked_post_id,
        current_asked_text=row.current_asked_text,
        current_dedupe_key=row.current_dedupe_key,
        last_fragment_at=row.last_fragment_at,
        coalesce_window_seconds=row.coalesce_window_seconds,
        asked_at=row.asked_at,
        deadline_at=row.deadline_at,
        closed_at=row.closed_at,
        next_planner_run_at=row.next_planner_run_at,
        planner_calls_count=row.planner_calls_count,
        send_retry_count=row.send_retry_count,
    )


def _row_to_step(row: GoalStepRow) -> GoalStep:
    try:
        kind = GoalStepKind(row.kind)
    except ValueError:
        kind = GoalStepKind.NOTE
    return GoalStep(
        id=row.id,
        goal_id=row.goal_id,
        seq=row.seq,
        kind=kind,
        timestamp=row.timestamp,
        text=row.text or "",
        target_username=row.target_username,
        target_user_id=row.target_user_id,
        metadata=dict(row.metadata_json or {}),
    )


def _aware(dt: datetime | None) -> datetime | None:
    """Normalise possibly-naive SQLite datetimes to UTC-aware."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# --- Repository ---


class GoalRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ---------------------------------------------------------------- create

    async def create_goal(
        self,
        *,
        plan_id: int | None,
        tracker: str,
        task_external_id: str,
        description: str,
        why_it_matters: str,
        initial_contact_hint: str,
        coalesce_window_seconds: int,
        deadline_at: datetime,
        parent_goal_id: int | None = None,
        depth: int = 0,
    ) -> ClarificationGoal:
        async with session_scope(self._session_factory) as session:
            row = GoalRow(
                plan_id=plan_id,
                tracker=tracker,
                task_external_id=task_external_id,
                description=description,
                why_it_matters=why_it_matters,
                initial_contact_hint=initial_contact_hint,
                state=GoalState.PENDING.value,
                coalesce_window_seconds=coalesce_window_seconds,
                deadline_at=deadline_at,
                parent_goal_id=parent_goal_id,
                depth=depth,
            )
            session.add(row)
            await session.flush()
            return _row_to_goal(row)

    # ---------------------------------------------------------------- read

    async def get(self, goal_id: int) -> ClarificationGoal | None:
        async with self._session_factory() as session:
            row = (await session.execute(
                select(GoalRow).where(GoalRow.id == goal_id)
            )).scalar_one_or_none()
        return _row_to_goal(row) if row is not None else None

    async def list_for_task(
        self, tracker: str, external_id: str,
    ) -> list[ClarificationGoal]:
        async with self._session_factory() as session:
            stmt = (
                select(GoalRow)
                .where(
                    GoalRow.tracker == tracker,
                    GoalRow.task_external_id == external_id,
                )
                .order_by(GoalRow.id.asc())
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [_row_to_goal(r) for r in rows]

    async def list_for_plan(self, plan_id: int) -> list[ClarificationGoal]:
        async with self._session_factory() as session:
            stmt = (
                select(GoalRow)
                .where(GoalRow.plan_id == plan_id)
                .order_by(GoalRow.id.asc())
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [_row_to_goal(r) for r in rows]

    async def list_top_level_for_plan(
        self, plan_id: int,
    ) -> list[ClarificationGoal]:
        """Top-level goals only (parent_goal_id IS NULL). Used by
        ``_maybe_resettle_plan`` so subgoal terminations don't trigger
        a premature replan.
        """
        async with self._session_factory() as session:
            stmt = (
                select(GoalRow)
                .where(
                    GoalRow.plan_id == plan_id,
                    GoalRow.parent_goal_id.is_(None),
                )
                .order_by(GoalRow.id.asc())
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [_row_to_goal(r) for r in rows]

    async def list_subgoals(self, parent_id: int) -> list[ClarificationGoal]:
        async with self._session_factory() as session:
            stmt = (
                select(GoalRow)
                .where(GoalRow.parent_goal_id == parent_id)
                .order_by(GoalRow.id.asc())
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [_row_to_goal(r) for r in rows]

    async def find_blocked_with_all_subgoals_terminal(
        self,
    ) -> list[ClarificationGoal]:
        """BLOCKED_ON_SUBGOAL goals whose every child is in a terminal
        state (ACHIEVED / ABANDONED / ESCALATED). Sweep flips them to
        READY_TO_REPLAN so the parent's planner can react."""
        terminal = [s.value for s in (
            GoalState.ACHIEVED, GoalState.ABANDONED, GoalState.ESCALATED,
        )]
        async with self._session_factory() as session:
            blocked = list((await session.execute(
                select(GoalRow).where(
                    GoalRow.state == GoalState.BLOCKED_ON_SUBGOAL.value,
                )
            )).scalars().all())
            ready: list[GoalRow] = []
            for parent in blocked:
                subgoals = list((await session.execute(
                    select(GoalRow.state)
                    .where(GoalRow.parent_goal_id == parent.id)
                )).scalars().all())
                if not subgoals:
                    # Parent blocked but has no children — bug; let the
                    # sweep unblock it so the planner notices and decides.
                    ready.append(parent)
                    continue
                if all(s in terminal for s in subgoals):
                    ready.append(parent)
        return [_row_to_goal(r) for r in ready]

    async def find_active_by_thread(
        self, asked_post_id: str,
    ) -> ClarificationGoal | None:
        active = [s.value for s in ACTIVE_STATES]
        async with self._session_factory() as session:
            stmt = (
                select(GoalRow)
                .where(
                    GoalRow.current_asked_post_id == asked_post_id,
                    GoalRow.state.in_(active),
                )
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
        return _row_to_goal(row) if row is not None else None

    async def find_active_by_channel(
        self, mm_channel_id: str, mm_user_id: str,
    ) -> ClarificationGoal | None:
        """Plain DM (no thread root): pick the goal with the most
        recent ``current_asked_post_id`` for this DM channel + user.

        We pick MOST recent (not oldest) because the human is most
        likely answering the question we just asked, not a stale one.
        """
        active = [s.value for s in ACTIVE_STATES]
        async with self._session_factory() as session:
            stmt = (
                select(GoalRow)
                .where(
                    GoalRow.current_channel_id == mm_channel_id,
                    GoalRow.current_target_user_id == mm_user_id,
                    GoalRow.state.in_(active),
                )
                .order_by(GoalRow.asked_at.desc())
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
        return _row_to_goal(row) if row is not None else None

    async def list_steps(self, goal_id: int) -> list[GoalStep]:
        async with self._session_factory() as session:
            stmt = (
                select(GoalStepRow)
                .where(GoalStepRow.goal_id == goal_id)
                .order_by(GoalStepRow.seq.asc())
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [_row_to_step(r) for r in rows]

    async def existing_descriptions_for_plan(self, plan_id: int) -> set[str]:
        """Top-level descriptions still active on this plan. Used by
        ``request_clarifications`` to skip re-creating the same
        question when the analyst replans (subgoals are excluded —
        they're internal decomposition, not analyst-authored)."""
        active = [s.value for s in ACTIVE_STATES]
        async with self._session_factory() as session:
            stmt = (
                select(GoalRow.description)
                .where(
                    GoalRow.plan_id == plan_id,
                    GoalRow.state.in_(active),
                    GoalRow.parent_goal_id.is_(None),
                )
            )
            return set((await session.execute(stmt)).scalars().all())

    # ---------------------------------------------------------------- coalescer queries

    async def find_idle_coalescing(
        self, *, now: datetime,
    ) -> list[ClarificationGoal]:
        """Goals in COALESCING whose idle window has elapsed."""
        async with self._session_factory() as session:
            stmt = (
                select(GoalRow)
                .where(
                    GoalRow.state == GoalState.COALESCING.value,
                    GoalRow.last_fragment_at.is_not(None),
                )
                .order_by(GoalRow.last_fragment_at.asc())
            )
            rows = list((await session.execute(stmt)).scalars().all())
        out: list[ClarificationGoal] = []
        for row in rows:
            if row.last_fragment_at is None:
                continue
            last = _aware(row.last_fragment_at)
            assert last is not None
            if last + timedelta(seconds=row.coalesce_window_seconds) <= now:
                out.append(_row_to_goal(row))
        return out

    async def claim_for_replan(self, goal_id: int) -> ClarificationGoal | None:
        """Atomic flip COALESCING / READY_TO_REPLAN → REPLANNING.

        Returns the goal if we won the flip, else None (someone else
        won). Used by the orchestrator to acquire a soft-lock before
        calling the planner.
        """
        async with session_scope(self._session_factory) as session:
            now = datetime.now(timezone.utc)
            stmt = (
                update(GoalRow)
                .where(
                    GoalRow.id == goal_id,
                    GoalRow.state.in_([
                        GoalState.COALESCING.value,
                        GoalState.READY_TO_REPLAN.value,
                        GoalState.PENDING.value,
                    ]),
                )
                .values(
                    state=GoalState.REPLANNING.value,
                    last_planning_started_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            result = await session.execute(stmt)
            if result.rowcount == 0:
                return None
            row = (await session.execute(
                select(GoalRow).where(GoalRow.id == goal_id)
            )).scalar_one()
        return _row_to_goal(row)

    async def find_stuck_replanning(
        self, *, older_than: datetime,
    ) -> list[ClarificationGoal]:
        """REPLANNING for ≥ ``older_than - now`` seconds with no
        progress — assume planner crashed; revert via state update.
        """
        async with self._session_factory() as session:
            stmt = (
                select(GoalRow)
                .where(
                    GoalRow.state == GoalState.REPLANNING.value,
                    GoalRow.last_planning_started_at.is_not(None),
                    GoalRow.last_planning_started_at <= older_than,
                )
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [_row_to_goal(r) for r in rows]

    async def find_overdue(self, *, now: datetime) -> list[ClarificationGoal]:
        """Active goals whose ``deadline_at`` has passed."""
        active = [s.value for s in ACTIVE_STATES]
        async with self._session_factory() as session:
            stmt = (
                select(GoalRow)
                .where(
                    GoalRow.state.in_(active),
                    GoalRow.deadline_at.is_not(None),
                )
            )
            rows = list((await session.execute(stmt)).scalars().all())
        out: list[ClarificationGoal] = []
        for row in rows:
            deadline = _aware(row.deadline_at)
            if deadline is not None and deadline <= now:
                out.append(_row_to_goal(row))
        return out

    async def find_due_waiting(
        self, *, now: datetime,
    ) -> list[ClarificationGoal]:
        """WAITING goals whose ``next_planner_run_at`` has passed."""
        async with self._session_factory() as session:
            stmt = (
                select(GoalRow)
                .where(
                    GoalRow.state == GoalState.WAITING.value,
                    GoalRow.next_planner_run_at.is_not(None),
                )
            )
            rows = list((await session.execute(stmt)).scalars().all())
        out: list[ClarificationGoal] = []
        for row in rows:
            due = _aware(row.next_planner_run_at)
            if due is not None and due <= now:
                out.append(_row_to_goal(row))
        return out

    async def find_pending_send(self) -> list[ClarificationGoal]:
        """Goals stuck in SEND_PENDING (Communicator failed to send,
        we want to retry on next tick).
        """
        async with self._session_factory() as session:
            stmt = select(GoalRow).where(
                GoalRow.state == GoalState.SEND_PENDING.value,
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [_row_to_goal(r) for r in rows]

    async def race_safe_check_no_new_fragment(
        self, goal_id: int, since: datetime,
    ) -> bool:
        """Return True iff the goal received NO fragment after
        ``since``. Used by ``deadline_sweep`` to avoid abandoning a
        goal that received a reply between the SELECT and the flip.
        """
        async with self._session_factory() as session:
            row = (await session.execute(
                select(GoalRow).where(GoalRow.id == goal_id)
            )).scalar_one_or_none()
            if row is None:
                return True
            last = _aware(row.last_fragment_at)
            if last is None:
                return True
            return last <= since

    # ---------------------------------------------------------------- mutate

    async def update_state(
        self,
        goal_id: int,
        new_state: GoalState,
        *,
        last_fragment_at: datetime | None = None,
        clear_outstanding: bool = False,
        outstanding_post_id: str | None = None,
        outstanding_user_id: str | None = None,
        outstanding_username: str | None = None,
        outstanding_channel: str | None = None,
        outstanding_text: str | None = None,
        outstanding_dedupe_key: str | None = None,
        next_planner_run_at: datetime | None = None,
        clear_next_planner_run_at: bool = False,
        final_answer: str | None = None,
        closed: bool = False,
        increment_planner_calls: bool = False,
        increment_send_retry: bool = False,
        clear_send_retry: bool = False,
        clear_planning_started: bool = False,
    ) -> None:
        async with session_scope(self._session_factory) as session:
            row = (await session.execute(
                select(GoalRow).where(GoalRow.id == goal_id)
            )).scalar_one_or_none()
            if row is None:
                return
            row.state = new_state.value
            if last_fragment_at is not None:
                row.last_fragment_at = last_fragment_at
            if clear_outstanding:
                row.current_target_user_id = None
                row.current_target_username = None
                row.current_channel_id = None
                row.current_asked_post_id = None
                row.current_asked_text = None
                row.current_dedupe_key = None
                row.last_fragment_at = None
            if outstanding_post_id is not None:
                row.current_asked_post_id = outstanding_post_id
            if outstanding_user_id is not None:
                row.current_target_user_id = outstanding_user_id
            if outstanding_username is not None:
                row.current_target_username = outstanding_username
            if outstanding_channel is not None:
                row.current_channel_id = outstanding_channel
            if outstanding_text is not None:
                row.current_asked_text = outstanding_text
            if outstanding_dedupe_key is not None:
                row.current_dedupe_key = outstanding_dedupe_key
            if next_planner_run_at is not None:
                row.next_planner_run_at = next_planner_run_at
            if clear_next_planner_run_at:
                row.next_planner_run_at = None
            if final_answer is not None:
                row.final_answer = final_answer
            if closed and row.closed_at is None:
                row.closed_at = datetime.now(timezone.utc)
            if increment_planner_calls:
                row.planner_calls_count = (row.planner_calls_count or 0) + 1
            if increment_send_retry:
                row.send_retry_count = (row.send_retry_count or 0) + 1
            if clear_send_retry:
                row.send_retry_count = 0
            if clear_planning_started:
                row.last_planning_started_at = None

    async def set_outstanding(
        self,
        goal_id: int,
        *,
        target_user_id: str,
        target_username: str | None,
        channel_id: str,
        asked_post_id: str,
        asked_text: str,
        dedupe_key: str | None,
        new_state: GoalState = GoalState.AWAITING_REPLY,
    ) -> None:
        await self.update_state(
            goal_id, new_state,
            outstanding_user_id=target_user_id,
            outstanding_username=target_username,
            outstanding_channel=channel_id,
            outstanding_post_id=asked_post_id,
            outstanding_text=asked_text,
            outstanding_dedupe_key=dedupe_key,
            clear_send_retry=True,
            clear_planning_started=True,
        )

    # ---------------------------------------------------------------- steps

    async def append_step(
        self,
        *,
        goal_id: int,
        kind: GoalStepKind,
        text: str,
        target_username: str | None = None,
        target_user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> GoalStep:
        async with session_scope(self._session_factory) as session:
            # Compute next seq atomically — keep simple SELECT max +1
            # (single-process workload, UNIQUE(goal_id, seq) catches
            # any race retroactively).
            current_max = (await session.execute(
                select(func.max(GoalStepRow.seq)).where(GoalStepRow.goal_id == goal_id)
            )).scalar()
            next_seq = (current_max or 0) + 1
            row = GoalStepRow(
                goal_id=goal_id,
                seq=next_seq,
                kind=kind.value,
                text=text,
                target_username=target_username,
                target_user_id=target_user_id,
                metadata_json=metadata or {},
            )
            session.add(row)
            await session.flush()
            return _row_to_step(row)

    # ---------------------------------------------------------------- fragments

    async def append_fragment(
        self,
        *,
        goal_id: int,
        mm_post_id: str,
        asked_post_id: str | None,
        text: str,
        received_at: datetime,
    ) -> bool:
        """Buffer a fragment. Returns False on duplicate (UNIQUE per
        ``(goal_id, mm_post_id)``).

        Also stamps ``last_fragment_at`` and flips state to COALESCING
        if the goal was AWAITING_REPLY. We do NOT flip from REPLANNING
        — the planner is mid-call and the fragment will be picked up
        on the next ASK.
        """
        async with session_scope(self._session_factory) as session:
            frag = GoalFragmentRow(
                goal_id=goal_id,
                mm_post_id=mm_post_id,
                asked_post_id=asked_post_id,
                text=text,
                received_at=received_at,
            )
            session.add(frag)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                return False
            goal_row = (await session.execute(
                select(GoalRow).where(GoalRow.id == goal_id)
            )).scalar_one_or_none()
            if goal_row is not None:
                goal_row.last_fragment_at = received_at
                if goal_row.state == GoalState.AWAITING_REPLY.value:
                    goal_row.state = GoalState.COALESCING.value
            return True

    async def list_unflushed_fragments(
        self, goal_id: int,
    ) -> list[GoalFragmentRow]:
        async with self._session_factory() as session:
            stmt = (
                select(GoalFragmentRow)
                .where(
                    GoalFragmentRow.goal_id == goal_id,
                    GoalFragmentRow.flushed.is_(False),
                )
                .order_by(GoalFragmentRow.received_at.asc())
            )
            return list((await session.execute(stmt)).scalars().all())

    async def mark_fragments_flushed(self, goal_id: int) -> None:
        async with session_scope(self._session_factory) as session:
            rows = list((await session.execute(
                select(GoalFragmentRow)
                .where(
                    GoalFragmentRow.goal_id == goal_id,
                    GoalFragmentRow.flushed.is_(False),
                )
            )).scalars().all())
            for row in rows:
                row.flushed = True

    async def archive_unflushed_as_stale(self, goal_id: int) -> int:
        """When the planner spawns a new ASK before the previous
        question's fragments have been coalesced, those fragments are
        no longer evidence about the new question. We archive them as
        ``STALE_FRAGMENT`` steps so the audit trail keeps them, then
        drop the rows from the buffer.
        """
        async with session_scope(self._session_factory) as session:
            rows = list((await session.execute(
                select(GoalFragmentRow)
                .where(
                    GoalFragmentRow.goal_id == goal_id,
                    GoalFragmentRow.flushed.is_(False),
                )
                .order_by(GoalFragmentRow.received_at.asc())
            )).scalars().all())
            for row in rows:
                # Append as a stale-fragment step — we cheat here and
                # do an inline SELECT max(seq); for an archive sweep
                # the cost is acceptable.
                current_max = (await session.execute(
                    select(func.max(GoalStepRow.seq))
                    .where(GoalStepRow.goal_id == goal_id)
                )).scalar()
                next_seq = (current_max or 0) + 1
                session.add(GoalStepRow(
                    goal_id=goal_id, seq=next_seq,
                    kind=GoalStepKind.STALE_FRAGMENT.value,
                    text=row.text,
                    timestamp=row.received_at,
                    metadata_json={"mm_post_id": row.mm_post_id},
                ))
                row.flushed = True
            return len(rows)


__all__ = ["GoalRepository"]
