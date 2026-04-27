"""Repository for the analyst's per-ticket session state (Phase 5.0).

Owns SQL against ``tasks.awaiting_*`` (the single source of truth for
"is the analyst waiting on a human reply?") and the conversation
log tables ``analyst_conversation_steps`` /
``analyst_conversation_fragments``.

Replaces ``ClarificationTaskRepository`` from Phase 4.6 — there's no
longer a separate ClarificationTask entity. The TaskRow IS the
session.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.domain.models.analyst_conversation import (
    ConversationStep,
    ConversationStepKind,
)
from virtual_dev.infrastructure.db import (
    AnalystConversationFragmentRow,
    AnalystConversationStepRow,
    TaskRow,
)
from virtual_dev.infrastructure.db.base import session_scope


def _row_to_step(row: AnalystConversationStepRow) -> ConversationStep:
    try:
        kind = ConversationStepKind(row.kind)
    except ValueError:
        kind = ConversationStepKind.NOTE
    return ConversationStep(
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


class AnalystSessionRepository:
    """Owns the per-task session state + conversation log."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ---------------------------------------------------------------- read
    async def get_task(self, task_id: int) -> TaskRow | None:
        async with self._session_factory() as session:
            return (await session.execute(
                select(TaskRow).where(TaskRow.id == task_id)
            )).scalar_one_or_none()

    async def find_by_awaiting_post_id(
        self, asked_post_id: str,
    ) -> TaskRow | None:
        async with self._session_factory() as session:
            return (await session.execute(
                select(TaskRow).where(
                    TaskRow.awaiting_post_id == asked_post_id,
                ).limit(1)
            )).scalar_one_or_none()

    async def find_by_awaiting_channel(
        self, mm_channel_id: str, mm_user_id: str,
    ) -> TaskRow | None:
        """Plain-DM routing: pick the most recently-asked task in
        this channel for this user."""
        async with self._session_factory() as session:
            return (await session.execute(
                select(TaskRow)
                .where(
                    TaskRow.awaiting_channel_id == mm_channel_id,
                    TaskRow.awaiting_user_id == mm_user_id,
                )
                .order_by(TaskRow.id.desc())
                .limit(1)
            )).scalar_one_or_none()

    async def find_idle_awaiting(
        self, *, now: datetime,
    ) -> list[TaskRow]:
        """Tasks whose ``last_fragment_at`` + coalesce window has elapsed."""
        async with self._session_factory() as session:
            rows = list((await session.execute(
                select(TaskRow).where(
                    TaskRow.awaiting_post_id.is_not(None),
                    TaskRow.last_fragment_at.is_not(None),
                )
            )).scalars().all())
        out: list[TaskRow] = []
        for row in rows:
            last = _aware(row.last_fragment_at)
            if last is None:
                continue
            if last + timedelta(seconds=row.coalesce_window_seconds) <= now:
                out.append(row)
        return out

    async def find_overdue(
        self, *, now: datetime,
    ) -> list[TaskRow]:
        """Tasks whose ``analyst_deadline_at`` has passed and aren't
        already terminal."""
        async with self._session_factory() as session:
            rows = list((await session.execute(
                select(TaskRow).where(
                    TaskRow.analyst_deadline_at.is_not(None),
                    TaskRow.internal_status.notin_(
                        ["ready", "failed", "done"],
                    ),
                )
            )).scalars().all())
        out: list[TaskRow] = []
        for row in rows:
            deadline = _aware(row.analyst_deadline_at)
            if deadline is not None and deadline <= now:
                out.append(row)
        return out

    # ---------------------------------------------------------------- mutate
    async def install_awaiting(
        self,
        task_id: int,
        *,
        post_id: str,
        user_id: str,
        username: str | None,
        channel_id: str,
        dedupe_key: str | None,
    ) -> None:
        async with session_scope(self._session_factory) as session:
            row = (await session.execute(
                select(TaskRow).where(TaskRow.id == task_id)
            )).scalar_one_or_none()
            if row is None:
                return
            row.awaiting_post_id = post_id
            row.awaiting_user_id = user_id
            row.awaiting_username = username
            row.awaiting_channel_id = channel_id
            row.awaiting_dedupe_key = dedupe_key
            row.last_fragment_at = None

    async def clear_awaiting(self, task_id: int) -> None:
        async with session_scope(self._session_factory) as session:
            row = (await session.execute(
                select(TaskRow).where(TaskRow.id == task_id)
            )).scalar_one_or_none()
            if row is None:
                return
            row.awaiting_post_id = None
            row.awaiting_user_id = None
            row.awaiting_username = None
            row.awaiting_channel_id = None
            row.awaiting_dedupe_key = None
            row.last_fragment_at = None

    async def increment_iteration(
        self,
        task_id: int,
        *,
        deadline_at: datetime | None = None,
        coalesce_window_seconds: int | None = None,
    ) -> None:
        async with session_scope(self._session_factory) as session:
            row = (await session.execute(
                select(TaskRow).where(TaskRow.id == task_id)
            )).scalar_one_or_none()
            if row is None:
                return
            row.analyst_iteration_count = (row.analyst_iteration_count or 0) + 1
            row.last_analyst_started_at = datetime.now(timezone.utc)
            if deadline_at is not None and row.analyst_deadline_at is None:
                row.analyst_deadline_at = deadline_at
            if coalesce_window_seconds is not None:
                row.coalesce_window_seconds = coalesce_window_seconds

    async def clear_started_at(self, task_id: int) -> None:
        async with session_scope(self._session_factory) as session:
            row = (await session.execute(
                select(TaskRow).where(TaskRow.id == task_id)
            )).scalar_one_or_none()
            if row is None:
                return
            row.last_analyst_started_at = None

    # ---------------------------------------------------------------- steps
    async def append_step(
        self,
        *,
        task_id: int,
        kind: ConversationStepKind,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> ConversationStep:
        async with session_scope(self._session_factory) as session:
            current_max = (await session.execute(
                select(func.max(AnalystConversationStepRow.seq))
                .where(AnalystConversationStepRow.task_id == task_id)
            )).scalar()
            next_seq = (current_max or 0) + 1
            row = AnalystConversationStepRow(
                task_id=task_id,
                seq=next_seq,
                kind=kind.value,
                text=text,
                metadata_json=metadata or {},
            )
            session.add(row)
            await session.flush()
            return _row_to_step(row)

    async def list_steps(self, task_id: int) -> list[ConversationStep]:
        async with self._session_factory() as session:
            stmt = (
                select(AnalystConversationStepRow)
                .where(AnalystConversationStepRow.task_id == task_id)
                .order_by(AnalystConversationStepRow.seq.asc())
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
        """Buffer a fragment. Returns False on duplicate. Stamps last_fragment_at."""
        async with session_scope(self._session_factory) as session:
            frag = AnalystConversationFragmentRow(
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
                select(TaskRow).where(TaskRow.id == task_id)
            )).scalar_one_or_none()
            if row is not None:
                row.last_fragment_at = received_at
            return True

    async def list_unflushed_fragments(
        self, task_id: int,
    ) -> list[AnalystConversationFragmentRow]:
        async with self._session_factory() as session:
            stmt = (
                select(AnalystConversationFragmentRow)
                .where(
                    AnalystConversationFragmentRow.task_id == task_id,
                    AnalystConversationFragmentRow.flushed.is_(False),
                )
                .order_by(AnalystConversationFragmentRow.received_at.asc())
            )
            return list((await session.execute(stmt)).scalars().all())

    async def mark_fragments_flushed(self, task_id: int) -> None:
        async with session_scope(self._session_factory) as session:
            rows = list((await session.execute(
                select(AnalystConversationFragmentRow)
                .where(
                    AnalystConversationFragmentRow.task_id == task_id,
                    AnalystConversationFragmentRow.flushed.is_(False),
                )
            )).scalars().all())
            for row in rows:
                row.flushed = True


__all__ = ["AnalystSessionRepository"]
