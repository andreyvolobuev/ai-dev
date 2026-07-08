"""Wipe a ticket's persisted state so the bot re-processes it from scratch.

Backs the team-lead ``/reset <TICKET>`` DM command. It clears every DB row the
bot holds for a ticket — the task, its analyst conversation memory, its plans,
and its merge-request projections — but touches NOTHING in Jira or GitLab. The
ticket only gets re-picked-up when it re-enters the poller's JQL
(``status = "To Do"``) on a later tick, which a human controls.

Rows are linked logically by ``(tracker, external_id)`` / ``task_external_id``,
not by DB foreign keys, so each table is cleared explicitly. The analyst
conversation rows key on the task's integer id, resolved before the task row is
deleted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Delete, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from virtual_dev.infrastructure.db.models import (
    AgentMessageRow,
    AnalystConversationFragmentRow,
    AnalystConversationStepRow,
    MergeRequestRow,
    PlanRow,
    TaskRow,
    TicketResetRow,
)


@dataclass(frozen=True)
class ResetSummary:
    """Per-table delete counts. ``found`` is False when nothing existed for the
    ticket (so the caller can say "nothing to reset" instead of "done")."""

    found: bool
    tasks: int = 0
    conversation_steps: int = 0
    conversation_fragments: int = 0
    plans: int = 0
    merge_requests: int = 0
    bus_messages: int = 0

    @property
    def total(self) -> int:
        return (
            self.tasks
            + self.conversation_steps
            + self.conversation_fragments
            + self.plans
            + self.merge_requests
            + self.bus_messages
        )


async def reset_ticket_state(
    session: AsyncSession, *, tracker: str, external_id: str,
) -> ResetSummary:
    """Delete all rows the bot stores for ``(tracker, external_id)``.

    Idempotent: a second call (or a call for an unknown ticket) deletes nothing
    and returns ``found=False``. Runs inside the caller's transaction.
    """
    task_ids = list((await session.execute(
        select(TaskRow.id).where(
            TaskRow.tracker == tracker,
            TaskRow.external_id == external_id,
        )
    )).scalars().all())

    steps = frags = 0
    if task_ids:
        steps = await _delete(session, delete(AnalystConversationStepRow).where(
            AnalystConversationStepRow.task_id.in_(task_ids),
        ))
        frags = await _delete(session, delete(AnalystConversationFragmentRow).where(
            AnalystConversationFragmentRow.task_id.in_(task_ids),
        ))

    # Plans/MRs key on the external id (not the task's row id), so they are
    # cleared even if the task row itself was already gone — a partial prior
    # reset shouldn't leave orphans behind.
    plans = await _delete(session, delete(PlanRow).where(
        PlanRow.tracker == tracker,
        PlanRow.task_external_id == external_id,
    ))
    mrs = await _delete(session, delete(MergeRequestRow).where(
        MergeRequestRow.task_external_id == external_id,
    ))
    tasks = await _delete(session, delete(TaskRow).where(
        TaskRow.tracker == tracker,
        TaskRow.external_id == external_id,
    ))

    # Un-consumed bus messages for this ticket would otherwise fire
    # later against the wiped state. payload_json is a JSON column, so
    # match in Python — the unconsumed inbox is small.
    pending = list((await session.execute(
        select(AgentMessageRow).where(AgentMessageRow.consumed_at.is_(None))
    )).scalars().all())
    bus = 0
    for msg in pending:
        payload = msg.payload_json or {}
        if str(payload.get("external_id") or "") != external_id:
            continue
        if str(payload.get("tracker") or tracker) != tracker:
            continue
        await session.delete(msg)
        bus += 1

    # Leave the marker LAST so a run in flight during this reset — which
    # started before `now` — discards its late writes (save_plan / dev
    # push+MR check the marker against their own start time).
    now = datetime.now(timezone.utc)
    marker = (await session.execute(
        select(TicketResetRow).where(
            TicketResetRow.tracker == tracker,
            TicketResetRow.external_id == external_id,
        )
    )).scalar_one_or_none()
    if marker is None:
        session.add(TicketResetRow(
            tracker=tracker, external_id=external_id, reset_at=now,
        ))
    else:
        marker.reset_at = now

    found = bool(tasks or steps or frags or plans or mrs or bus)
    return ResetSummary(
        found=found,
        tasks=tasks,
        conversation_steps=steps,
        conversation_fragments=frags,
        plans=plans,
        merge_requests=mrs,
        bus_messages=bus,
    )


async def was_reset_since(
    session: AsyncSession, *, tracker: str, external_id: str, since: datetime,
) -> bool:
    """True when a ``/reset`` for the ticket happened after ``since``.

    Long-running agent code calls this with its own start time right
    before persisting results — a positive answer means the lead wiped
    the ticket mid-run and the run's output must be discarded."""
    marker = (await session.execute(
        select(TicketResetRow).where(
            TicketResetRow.tracker == tracker,
            TicketResetRow.external_id == external_id,
        )
    )).scalar_one_or_none()
    if marker is None:
        return False
    reset_at = marker.reset_at
    if reset_at.tzinfo is None:
        reset_at = reset_at.replace(tzinfo=timezone.utc)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return reset_at >= since


async def _delete(session: AsyncSession, stmt: Delete) -> int:
    result = await session.execute(stmt)
    # A DELETE yields a CursorResult with .rowcount, but AsyncSession.execute is
    # typed as the base Result[Any] (no rowcount in the stubs) — read it off
    # duck-typed rather than casting the whole result.
    return int(getattr(result, "rowcount", 0) or 0)
