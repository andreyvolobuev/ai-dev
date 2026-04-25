"""Repository: only the file that may touch ``questions``/``question_*`` rows.

Keeping all SQL in one place makes the orchestrator easy to test (we
swap the repo with an in-memory fake), keeps row→domain mapping in one
spot, and lets the hot coalescer query stay readable.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.domain.models.clarification import (
    ACTIVE_STATES,
    Answer,
    AnswerFragment,
    Classification,
    Question,
    QuestionState,
    Stakeholder,
    StakeholderKind,
)
from virtual_dev.infrastructure.db import (
    QuestionAnswerRow,
    QuestionFragmentRow,
    QuestionRow,
)
from virtual_dev.infrastructure.db.base import session_scope


class QuestionRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    # --- Insert / update ---

    async def create_root(
        self,
        *,
        tracker: str,
        task_external_id: str,
        plan_id: int,
        text: str,
        why_it_matters: str,
        stakeholder: Stakeholder,
        coalesce_window_seconds: int,
        deadline_at: datetime,
    ) -> Question:
        """Insert a root Question (chain_depth=0, parent_id=None).

        ``root_id`` initially equals ``id``; we set it after the row is
        flushed so the autoincrement id is known.
        """
        async with session_scope(self._session_factory) as session:
            row = QuestionRow(
                tracker=tracker,
                task_external_id=task_external_id,
                plan_id=plan_id,
                root_id=0,           # patched after flush
                parent_id=None,
                chain_depth=0,
                state=QuestionState.PENDING.value,
                text=text,
                why_it_matters=why_it_matters,
                coalesce_window_seconds=coalesce_window_seconds,
                deadline_at=deadline_at,
                **_stakeholder_columns(stakeholder),
            )
            session.add(row)
            await session.flush()
            row.root_id = row.id
            await session.flush()
            return _row_to_question(row)

    async def create_child(
        self,
        *,
        parent: Question,
        text: str,
        why_it_matters: str,
        stakeholder: Stakeholder,
        coalesce_window_seconds: int,
        deadline_at: datetime,
    ) -> Question:
        """Insert a child Question. Inherits ``root_id`` and ``tracker``
        / ``task_external_id`` from parent; ``chain_depth = parent + 1``.
        """
        async with session_scope(self._session_factory) as session:
            row = QuestionRow(
                tracker=parent.tracker,
                task_external_id=parent.task_external_id,
                plan_id=None,
                root_id=parent.root_id,
                parent_id=parent.id,
                chain_depth=parent.chain_depth + 1,
                state=QuestionState.PENDING.value,
                text=text,
                why_it_matters=why_it_matters,
                coalesce_window_seconds=coalesce_window_seconds,
                deadline_at=deadline_at,
                **_stakeholder_columns(stakeholder),
            )
            session.add(row)
            await session.flush()
            return _row_to_question(row)

    async def update_state(
        self,
        question_id: int,
        new_state: QuestionState,
        *,
        last_fragment_at: datetime | None = None,
        asked_post_id: str | None = None,
        mm_user_id: str | None = None,
        mm_channel_id: str | None = None,
        closed: bool = False,
        stakeholder: Stakeholder | None = None,
    ) -> None:
        """Patch a row in-place. Pass only the fields actually changing."""
        async with session_scope(self._session_factory) as session:
            row = (await session.execute(
                select(QuestionRow).where(QuestionRow.id == question_id)
            )).scalar_one_or_none()
            if row is None:
                return
            row.state = new_state.value
            if last_fragment_at is not None:
                row.last_fragment_at = last_fragment_at
            if asked_post_id is not None:
                row.asked_post_id = asked_post_id
            if mm_user_id is not None:
                row.mm_user_id = mm_user_id
            if mm_channel_id is not None:
                row.mm_channel_id = mm_channel_id
            if stakeholder is not None:
                for key, value in _stakeholder_columns(stakeholder).items():
                    setattr(row, key, value)
            if closed and row.closed_at is None:
                row.closed_at = datetime.now(timezone.utc)

    # --- Fragments ---

    async def append_fragment(
        self,
        *,
        question_id: int,
        mm_post_id: str,
        text: str,
        received_at: datetime,
    ) -> bool:
        """Insert a fragment if not already there (idempotent on
        ``mm_post_id``). Also stamps ``last_fragment_at`` on the
        question. Returns ``True`` if newly inserted, ``False`` on
        duplicate.
        """
        async with session_scope(self._session_factory) as session:
            row = QuestionFragmentRow(
                question_id=question_id,
                mm_post_id=mm_post_id,
                text=text,
                received_at=received_at,
            )
            session.add(row)
            try:
                await session.flush()
            except IntegrityError:
                # Duplicate WS-delivery — collapse silently.
                await session.rollback()
                return False
            # Stamp parent question.
            q_row = (await session.execute(
                select(QuestionRow).where(QuestionRow.id == question_id)
            )).scalar_one_or_none()
            if q_row is not None:
                q_row.last_fragment_at = received_at
                if q_row.state in (
                    QuestionState.ASKING.value, QuestionState.PENDING.value,
                ):
                    q_row.state = QuestionState.COALESCING.value
            return True

    async def list_unflushed_fragments(
        self, question_id: int,
    ) -> list[AnswerFragment]:
        async with self._session_factory() as session:
            stmt = (
                select(QuestionFragmentRow)
                .where(
                    QuestionFragmentRow.question_id == question_id,
                    QuestionFragmentRow.flushed.is_(False),
                )
                .order_by(QuestionFragmentRow.received_at.asc())
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [
            AnswerFragment(
                mm_post_id=r.mm_post_id, text=r.text, received_at=r.received_at,
            )
            for r in rows
        ]

    async def mark_fragments_flushed(self, question_id: int) -> None:
        async with session_scope(self._session_factory) as session:
            rows = list((await session.execute(
                select(QuestionFragmentRow)
                .where(
                    QuestionFragmentRow.question_id == question_id,
                    QuestionFragmentRow.flushed.is_(False),
                )
            )).scalars().all())
            for row in rows:
                row.flushed = True

    # --- Answers ---

    async def save_answer(
        self,
        *,
        question_id: int,
        coalesced_text: str,
        classification: Classification,
        extracted: dict[str, Any],
        cost_usd: float,
    ) -> None:
        async with session_scope(self._session_factory) as session:
            row = QuestionAnswerRow(
                question_id=question_id,
                coalesced_text=coalesced_text,
                classification=classification.value,
                extracted_json=extracted,
                cost_usd=cost_usd,
            )
            session.add(row)

    # --- Reads ---

    async def get(self, question_id: int) -> Question | None:
        async with self._session_factory() as session:
            row = (await session.execute(
                select(QuestionRow).where(QuestionRow.id == question_id)
            )).scalar_one_or_none()
            if row is None:
                return None
            answer = await self._load_answer(session, row.id)
        q = _row_to_question(row)
        q.answer = answer
        return q

    async def find_active_by_thread(
        self, asked_post_id: str,
    ) -> Question | None:
        async with self._session_factory() as session:
            stmt = (
                select(QuestionRow)
                .where(
                    QuestionRow.asked_post_id == asked_post_id,
                    QuestionRow.state.in_([s.value for s in ACTIVE_STATES]),
                )
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            return _row_to_question(row) if row is not None else None

    async def find_active_by_channel(
        self, mm_channel_id: str, mm_user_id: str,
    ) -> Question | None:
        """Plain DM (no thread root): pick the OLDEST active question
        for this DM channel + this respondent. FIFO matches what the
        user just saw at the top of their DM.
        """
        async with self._session_factory() as session:
            stmt = (
                select(QuestionRow)
                .where(
                    QuestionRow.mm_channel_id == mm_channel_id,
                    QuestionRow.mm_user_id == mm_user_id,
                    QuestionRow.state.in_([s.value for s in ACTIVE_STATES]),
                )
                .order_by(QuestionRow.asked_at.asc())
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            return _row_to_question(row) if row is not None else None

    async def find_idle_coalescing(self, *, now: datetime) -> list[Question]:
        """Coalescer hot-query: questions in COALESCING with no fragment
        for ≥ ``coalesce_window_seconds`` seconds.

        SQLite has no DATE_SUB, so we filter at row level after pulling
        candidates by state. The composite index on
        ``(state, last_fragment_at)`` keeps the candidate set small.
        """
        async with self._session_factory() as session:
            stmt = (
                select(QuestionRow)
                .where(
                    QuestionRow.state == QuestionState.COALESCING.value,
                    QuestionRow.last_fragment_at.is_not(None),
                )
                .order_by(QuestionRow.last_fragment_at.asc())
            )
            rows = list((await session.execute(stmt)).scalars().all())
        idle: list[Question] = []
        for row in rows:
            assert row.last_fragment_at is not None
            last = _aware(row.last_fragment_at)
            if last + timedelta(seconds=row.coalesce_window_seconds) <= now:
                idle.append(_row_to_question(row))
        return idle

    async def find_overdue(self, *, now: datetime) -> list[Question]:
        """Deadline-sweep: active questions whose ``deadline_at`` is in
        the past.
        """
        async with self._session_factory() as session:
            stmt = (
                select(QuestionRow)
                .where(
                    QuestionRow.state.in_([s.value for s in ACTIVE_STATES]),
                    QuestionRow.deadline_at.is_not(None),
                )
            )
            rows = list((await session.execute(stmt)).scalars().all())
        overdue: list[Question] = []
        for row in rows:
            assert row.deadline_at is not None
            if _aware(row.deadline_at) <= now:
                overdue.append(_row_to_question(row))
        return overdue

    async def list_for_root(self, root_id: int) -> list[Question]:
        async with self._session_factory() as session:
            stmt = (
                select(QuestionRow)
                .where(QuestionRow.root_id == root_id)
                .order_by(QuestionRow.id.asc())
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [_row_to_question(row) for row in rows]

    async def list_roots_for_plan(self, plan_id: int) -> list[Question]:
        async with self._session_factory() as session:
            stmt = (
                select(QuestionRow)
                .where(
                    QuestionRow.plan_id == plan_id,
                    QuestionRow.parent_id.is_(None),
                )
                .order_by(QuestionRow.id.asc())
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [_row_to_question(row) for row in rows]

    async def list_for_task(
        self, tracker: str, external_id: str,
    ) -> list[Question]:
        """All questions for one Issue, in id order. CLI uses this to
        render the tree.
        """
        async with self._session_factory() as session:
            stmt = (
                select(QuestionRow)
                .where(
                    QuestionRow.tracker == tracker,
                    QuestionRow.task_external_id == external_id,
                )
                .order_by(QuestionRow.id.asc())
            )
            rows = list((await session.execute(stmt)).scalars().all())
            answers: dict[int, Answer | None] = {}
            for row in rows:
                answers[row.id] = await self._load_answer(session, row.id)
        out: list[Question] = []
        for row in rows:
            q = _row_to_question(row)
            q.answer = answers.get(q.id)
            out.append(q)
        return out

    async def count_in_root(self, root_id: int) -> int:
        async with self._session_factory() as session:
            stmt = select(func.count()).select_from(QuestionRow).where(
                QuestionRow.root_id == root_id,
            )
            return int((await session.execute(stmt)).scalar() or 0)

    async def chain_user_ids(self, question: Question) -> set[str]:
        """Walk the ancestor chain and return the set of resolved
        ``mm_user_id`` s on it. Used for cycle detection: if a redirect
        would resolve to a stakeholder already in this set, abort.
        """
        async with self._session_factory() as session:
            current_id: int | None = question.parent_id
            chain: set[str] = set()
            while current_id is not None:
                row = (await session.execute(
                    select(QuestionRow).where(QuestionRow.id == current_id)
                )).scalar_one_or_none()
                if row is None:
                    break
                if row.stakeholder_resolved_mm_user_id:
                    chain.add(row.stakeholder_resolved_mm_user_id)
                current_id = row.parent_id
            # Include the question itself's resolved user too — we don't
            # want a→b where a's stakeholder is b.
            if question.stakeholder.resolved_mm_user_id:
                chain.add(question.stakeholder.resolved_mm_user_id)
            return chain

    # --- Internals ---

    async def _load_answer(
        self, session: AsyncSession, question_id: int,
    ) -> Answer | None:
        row = (await session.execute(
            select(QuestionAnswerRow)
            .where(QuestionAnswerRow.question_id == question_id)
        )).scalar_one_or_none()
        if row is None:
            return None
        try:
            classification: Classification | None = Classification(row.classification)
        except ValueError:
            classification = None
            logger.warning(
                "QuestionRepository: unknown classification {!r} for question {}",
                row.classification, question_id,
            )
        return Answer(
            fragments=[],   # not loaded — we don't need them after classification
            coalesced_text=row.coalesced_text,
            classification=classification,
            extracted=dict(row.extracted_json or {}),
            classified_at=row.classified_at,
            cost_usd=float(row.cost_usd or 0.0),
        )


# --- helpers ---


def _stakeholder_columns(stakeholder: Stakeholder) -> dict[str, Any]:
    return {
        "stakeholder_kind": stakeholder.kind.value,
        "stakeholder_raw_hint": stakeholder.raw_hint,
        "stakeholder_resolved_mm_user_id": stakeholder.resolved_mm_user_id,
        "stakeholder_resolved_mm_channel_id": stakeholder.resolved_mm_channel_id,
        "stakeholder_display_name": stakeholder.display_name,
    }


def _row_to_question(row: QuestionRow) -> Question:
    try:
        state = QuestionState(row.state)
    except ValueError:
        # Defensive: an unrecognised state means a forward-incompatible
        # row (we wrote it from a future code version). Best we can do
        # is treat it as ABANDONED so the orchestrator stops touching it.
        state = QuestionState.ABANDONED
    try:
        kind = StakeholderKind(row.stakeholder_kind)
    except ValueError:
        kind = StakeholderKind.UNRESOLVED_NAME
    stakeholder = Stakeholder(
        kind=kind,
        raw_hint=row.stakeholder_raw_hint or "",
        resolved_mm_user_id=row.stakeholder_resolved_mm_user_id,
        resolved_mm_channel_id=row.stakeholder_resolved_mm_channel_id,
        display_name=row.stakeholder_display_name,
    )
    return Question(
        id=row.id,
        root_id=row.root_id,
        parent_id=row.parent_id,
        chain_depth=row.chain_depth,
        state=state,
        text=row.text,
        why_it_matters=row.why_it_matters or "",
        stakeholder=stakeholder,
        asked_post_id=row.asked_post_id,
        mm_user_id=row.mm_user_id,
        mm_channel_id=row.mm_channel_id,
        last_fragment_at=row.last_fragment_at,
        deadline_at=row.deadline_at,
        tracker=row.tracker,
        task_external_id=row.task_external_id,
        plan_id=row.plan_id,
        asked_at=row.asked_at,
        closed_at=row.closed_at,
        coalesce_window_seconds=row.coalesce_window_seconds,
    )


def _aware(dt: datetime) -> datetime:
    """Normalise possibly-naive SQLite datetimes to UTC-aware."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


__all__ = ["QuestionRepository"]
