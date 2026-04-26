"""Repository for the task-driven clarification subsystem (Phase 4.5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.domain.models.clarification_task import (
    ClarificationTask,
    TaskStep,
    TaskStepKind,
)
from virtual_dev.infrastructure.db import (
    TaskFragmentRow,
    TaskRowClar,
    TaskStepRow,
)
from virtual_dev.infrastructure.db.base import session_scope


def _row_to_task(row: TaskRowClar) -> ClarificationTask:
    return ClarificationTask(
        id=row.id,
        plan_id=row.plan_id,
        parent_id=row.parent_id,
        tracker=row.tracker,
        task_external_id=row.task_external_id,
        question=row.question,
        info_source=row.info_source,
        info_source_class=row.info_source_class,
        current_response=row.current_response,
        is_solved=bool(row.is_solved),
        final_answer=row.final_answer,
        confidence=float(row.confidence or 0.0),
        depth=int(row.depth or 0),
        iteration_count=int(row.iteration_count or 0),
        tools_tried=list(row.tools_tried_json or []),
        closed=bool(row.closed),
        awaiting_post_id=row.awaiting_post_id,
        awaiting_user_id=row.awaiting_user_id,
        awaiting_username=row.awaiting_username,
        awaiting_channel_id=row.awaiting_channel_id,
        awaiting_dedupe_key=row.awaiting_dedupe_key,
        last_fragment_at=row.last_fragment_at,
        coalesce_window_seconds=int(row.coalesce_window_seconds or 60),
        created_at=row.created_at,
        deadline_at=row.deadline_at,
        solved_at=row.solved_at,
        closed_at=row.closed_at,
        last_planning_started_at=row.last_planning_started_at,
        next_planner_run_at=row.next_planner_run_at,
    )


def _row_to_step(row: TaskStepRow) -> TaskStep:
    try:
        kind = TaskStepKind(row.kind)
    except ValueError:
        kind = TaskStepKind.NOTE
    return TaskStep(
        id=row.id,
        task_id=row.task_id,
        seq=row.seq,
        kind=kind,
        timestamp=row.timestamp,
        text=row.text or "",
        metadata=dict(row.metadata_json or {}),
    )


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class ClarificationTaskRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ---------------------------------------------------------------- CRUD
    async def create_task(
        self,
        *,
        plan_id: int | None,
        parent_id: int | None,
        tracker: str,
        task_external_id: str,
        question: str,
        info_source: str | None,
        info_source_class: str | None,
        coalesce_window_seconds: int,
        deadline_at: datetime,
        depth: int = 0,
    ) -> ClarificationTask:
        async with session_scope(self._session_factory) as session:
            row = TaskRowClar(
                plan_id=plan_id,
                parent_id=parent_id,
                tracker=tracker,
                task_external_id=task_external_id,
                question=question,
                info_source=info_source,
                info_source_class=info_source_class,
                coalesce_window_seconds=coalesce_window_seconds,
                deadline_at=deadline_at,
                depth=depth,
            )
            session.add(row)
            await session.flush()
            return _row_to_task(row)

    async def get(self, task_id: int) -> ClarificationTask | None:
        async with self._session_factory() as session:
            row = (await session.execute(
                select(TaskRowClar).where(TaskRowClar.id == task_id)
            )).scalar_one_or_none()
        return _row_to_task(row) if row is not None else None

    async def chain(self, task_id: int) -> list[ClarificationTask]:
        """Return the chain root → … → task_id. Bounded at 16 to avoid
        runaway loops if data is corrupt."""
        out: list[ClarificationTask] = []
        current_id: int | None = task_id
        seen: set[int] = set()
        while current_id is not None and current_id not in seen and len(out) < 16:
            seen.add(current_id)
            t = await self.get(current_id)
            if t is None:
                break
            out.append(t)
            current_id = t.parent_id
        return list(reversed(out))

    async def list_for_plan(self, plan_id: int) -> list[ClarificationTask]:
        async with self._session_factory() as session:
            stmt = (
                select(TaskRowClar)
                .where(TaskRowClar.plan_id == plan_id)
                .order_by(TaskRowClar.id.asc())
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [_row_to_task(r) for r in rows]

    async def list_top_level_for_plan(
        self, plan_id: int,
    ) -> list[ClarificationTask]:
        async with self._session_factory() as session:
            stmt = (
                select(TaskRowClar)
                .where(
                    TaskRowClar.plan_id == plan_id,
                    TaskRowClar.parent_id.is_(None),
                )
                .order_by(TaskRowClar.id.asc())
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [_row_to_task(r) for r in rows]

    async def list_for_task(
        self, tracker: str, external_id: str,
    ) -> list[ClarificationTask]:
        async with self._session_factory() as session:
            stmt = (
                select(TaskRowClar)
                .where(
                    TaskRowClar.tracker == tracker,
                    TaskRowClar.task_external_id == external_id,
                )
                .order_by(TaskRowClar.id.asc())
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [_row_to_task(r) for r in rows]

    async def list_subtasks(self, parent_id: int) -> list[ClarificationTask]:
        async with self._session_factory() as session:
            stmt = (
                select(TaskRowClar)
                .where(TaskRowClar.parent_id == parent_id)
                .order_by(TaskRowClar.id.asc())
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [_row_to_task(r) for r in rows]

    async def existing_questions_for_plan(self, plan_id: int) -> set[str]:
        async with self._session_factory() as session:
            stmt = (
                select(TaskRowClar.question)
                .where(
                    TaskRowClar.plan_id == plan_id,
                    TaskRowClar.parent_id.is_(None),
                    TaskRowClar.closed.is_(False),
                )
            )
            return set((await session.execute(stmt)).scalars().all())

    # ---------------------------------------------------------------- queries
    async def find_active_by_thread(
        self, asked_post_id: str,
    ) -> ClarificationTask | None:
        async with self._session_factory() as session:
            stmt = (
                select(TaskRowClar)
                .where(
                    TaskRowClar.awaiting_post_id == asked_post_id,
                    TaskRowClar.closed.is_(False),
                )
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
        return _row_to_task(row) if row is not None else None

    async def find_active_by_channel(
        self, mm_channel_id: str, mm_user_id: str,
    ) -> ClarificationTask | None:
        async with self._session_factory() as session:
            stmt = (
                select(TaskRowClar)
                .where(
                    TaskRowClar.awaiting_channel_id == mm_channel_id,
                    TaskRowClar.awaiting_user_id == mm_user_id,
                    TaskRowClar.closed.is_(False),
                )
                .order_by(TaskRowClar.created_at.desc())
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
        return _row_to_task(row) if row is not None else None

    async def find_idle_awaiting(
        self, *, now: datetime,
    ) -> list[ClarificationTask]:
        """Tasks with last_fragment_at older than their coalesce window."""
        async with self._session_factory() as session:
            stmt = (
                select(TaskRowClar)
                .where(
                    TaskRowClar.closed.is_(False),
                    TaskRowClar.last_fragment_at.is_not(None),
                )
            )
            rows = list((await session.execute(stmt)).scalars().all())
        out: list[ClarificationTask] = []
        for row in rows:
            last = _aware(row.last_fragment_at)
            if last is None:
                continue
            if last + timedelta(seconds=row.coalesce_window_seconds) <= now:
                out.append(_row_to_task(row))
        return out

    async def find_overdue(self, *, now: datetime) -> list[ClarificationTask]:
        async with self._session_factory() as session:
            stmt = (
                select(TaskRowClar)
                .where(
                    TaskRowClar.closed.is_(False),
                    TaskRowClar.deadline_at.is_not(None),
                )
            )
            rows = list((await session.execute(stmt)).scalars().all())
        out: list[ClarificationTask] = []
        for row in rows:
            deadline = _aware(row.deadline_at)
            if deadline is not None and deadline <= now:
                out.append(_row_to_task(row))
        return out

    async def find_due_waiting(
        self, *, now: datetime,
    ) -> list[ClarificationTask]:
        async with self._session_factory() as session:
            stmt = (
                select(TaskRowClar)
                .where(
                    TaskRowClar.closed.is_(False),
                    TaskRowClar.next_planner_run_at.is_not(None),
                )
            )
            rows = list((await session.execute(stmt)).scalars().all())
        out: list[ClarificationTask] = []
        for row in rows:
            due = _aware(row.next_planner_run_at)
            if due is not None and due <= now:
                out.append(_row_to_task(row))
        return out

    # ---------------------------------------------------------------- mutate
    async def update(
        self,
        task_id: int,
        *,
        question: str | None = None,
        info_source: str | None = None,
        info_source_class: str | None = None,
        current_response: str | None = None,
        clear_current_response: bool = False,
        is_solved: bool | None = None,
        final_answer: str | None = None,
        confidence: float | None = None,
        iteration_count_delta: int = 0,
        append_tool_tried: str | None = None,
        clear_tools_tried: bool = False,
        closed: bool | None = None,
        # Awaiting state
        clear_awaiting: bool = False,
        awaiting_post_id: str | None = None,
        awaiting_user_id: str | None = None,
        awaiting_username: str | None = None,
        awaiting_channel_id: str | None = None,
        awaiting_dedupe_key: str | None = None,
        last_fragment_at: datetime | None = None,
        # Lifecycle
        last_planning_started_at: datetime | None = None,
        clear_last_planning_started_at: bool = False,
        next_planner_run_at: datetime | None = None,
        clear_next_planner_run_at: bool = False,
        solved_at: datetime | None = None,
    ) -> None:
        async with session_scope(self._session_factory) as session:
            row = (await session.execute(
                select(TaskRowClar).where(TaskRowClar.id == task_id)
            )).scalar_one_or_none()
            if row is None:
                return
            now = datetime.now(timezone.utc)
            if question is not None:
                row.question = question
            if info_source is not None:
                row.info_source = info_source or None
            if info_source_class is not None:
                row.info_source_class = info_source_class or None
            if current_response is not None:
                row.current_response = current_response
            if clear_current_response:
                row.current_response = None
            if is_solved is not None:
                row.is_solved = is_solved
                if is_solved and row.solved_at is None:
                    row.solved_at = now
            if final_answer is not None:
                row.final_answer = final_answer
            if confidence is not None:
                row.confidence = confidence
            if iteration_count_delta:
                row.iteration_count = (row.iteration_count or 0) + iteration_count_delta
            if append_tool_tried:
                tried = list(row.tools_tried_json or [])
                tried.append(append_tool_tried)
                row.tools_tried_json = tried
            if clear_tools_tried:
                row.tools_tried_json = []
            if closed is not None:
                row.closed = closed
                if closed and row.closed_at is None:
                    row.closed_at = now
            if clear_awaiting:
                row.awaiting_post_id = None
                row.awaiting_user_id = None
                row.awaiting_username = None
                row.awaiting_channel_id = None
                row.awaiting_dedupe_key = None
                row.last_fragment_at = None
            if awaiting_post_id is not None:
                row.awaiting_post_id = awaiting_post_id
            if awaiting_user_id is not None:
                row.awaiting_user_id = awaiting_user_id
            if awaiting_username is not None:
                row.awaiting_username = awaiting_username
            if awaiting_channel_id is not None:
                row.awaiting_channel_id = awaiting_channel_id
            if awaiting_dedupe_key is not None:
                row.awaiting_dedupe_key = awaiting_dedupe_key
            if last_fragment_at is not None:
                row.last_fragment_at = last_fragment_at
            if last_planning_started_at is not None:
                row.last_planning_started_at = last_planning_started_at
            if clear_last_planning_started_at:
                row.last_planning_started_at = None
            if next_planner_run_at is not None:
                row.next_planner_run_at = next_planner_run_at
            if clear_next_planner_run_at:
                row.next_planner_run_at = None
            if solved_at is not None:
                row.solved_at = solved_at

    # ---------------------------------------------------------------- steps
    async def append_step(
        self,
        *,
        task_id: int,
        kind: TaskStepKind,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> TaskStep:
        async with session_scope(self._session_factory) as session:
            current_max = (await session.execute(
                select(func.max(TaskStepRow.seq)).where(
                    TaskStepRow.task_id == task_id,
                )
            )).scalar()
            next_seq = (current_max or 0) + 1
            row = TaskStepRow(
                task_id=task_id,
                seq=next_seq,
                kind=kind.value,
                text=text,
                metadata_json=metadata or {},
            )
            session.add(row)
            await session.flush()
            return _row_to_step(row)

    async def list_steps(self, task_id: int) -> list[TaskStep]:
        async with self._session_factory() as session:
            stmt = (
                select(TaskStepRow)
                .where(TaskStepRow.task_id == task_id)
                .order_by(TaskStepRow.seq.asc())
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [_row_to_step(r) for r in rows]

    # ---------------------------------------------------------------- fragments
    async def append_fragment(
        self,
        *,
        task_id: int,
        mm_post_id: str,
        asked_post_id: str | None,
        text: str,
        received_at: datetime,
    ) -> bool:
        """Buffer a fragment. Returns False on duplicate (UNIQUE per
        ``(task_id, mm_post_id)``). Stamps last_fragment_at."""
        async with session_scope(self._session_factory) as session:
            frag = TaskFragmentRow(
                task_id=task_id,
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
            row = (await session.execute(
                select(TaskRowClar).where(TaskRowClar.id == task_id)
            )).scalar_one_or_none()
            if row is not None:
                row.last_fragment_at = received_at
            return True

    async def list_unflushed_fragments(
        self, task_id: int,
    ) -> list[TaskFragmentRow]:
        async with self._session_factory() as session:
            stmt = (
                select(TaskFragmentRow)
                .where(
                    TaskFragmentRow.task_id == task_id,
                    TaskFragmentRow.flushed.is_(False),
                )
                .order_by(TaskFragmentRow.received_at.asc())
            )
            return list((await session.execute(stmt)).scalars().all())

    async def mark_fragments_flushed(self, task_id: int) -> None:
        async with session_scope(self._session_factory) as session:
            rows = list((await session.execute(
                select(TaskFragmentRow)
                .where(
                    TaskFragmentRow.task_id == task_id,
                    TaskFragmentRow.flushed.is_(False),
                )
            )).scalars().all())
            for row in rows:
                row.flushed = True


__all__ = ["ClarificationTaskRepository"]
