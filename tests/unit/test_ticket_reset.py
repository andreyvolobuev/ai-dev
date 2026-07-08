"""reset_ticket_state clears exactly one ticket's rows, nothing else."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.services.ticket_reset import reset_ticket_state
from virtual_dev.infrastructure.db.base import session_scope
from virtual_dev.infrastructure.db.models import (
    AnalystConversationFragmentRow,
    AnalystConversationStepRow,
    MergeRequestRow,
    PlanRow,
    TaskRow,
)


async def _seed(session: AsyncSession, ticket: str, iid: int) -> int:
    task = TaskRow(tracker="jira", external_id=ticket, title=f"t-{ticket}")
    session.add(task)
    await session.flush()
    session.add_all([
        PlanRow(tracker="jira", task_external_id=ticket),
        MergeRequestRow(
            repo_key="repo", iid=iid, external_id=str(iid),
            task_external_id=ticket, title="mr", source_branch="b",
            target_branch="master", author_username="bot", web_url="http://x",
        ),
        AnalystConversationStepRow(task_id=task.id, seq=0, kind="NOTE"),
        AnalystConversationFragmentRow(task_id=task.id, mm_post_id=f"p-{ticket}"),
    ])
    return task.id


async def _count(session: AsyncSession, model: type) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


@pytest.mark.asyncio
async def test_reset_clears_only_the_target_ticket(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        await _seed(session, "DM-1", iid=1)
        await _seed(session, "DM-2", iid=2)

    async with session_scope(session_factory) as session:
        summary = await reset_ticket_state(session, tracker="jira", external_id="DM-1")

    assert summary.found is True
    assert (summary.tasks, summary.plans, summary.merge_requests) == (1, 1, 1)
    assert summary.conversation_steps == 1
    assert summary.conversation_fragments == 1
    assert summary.total == 5

    # DM-2 is untouched: exactly one row survives in every table.
    async with session_scope(session_factory) as session:
        assert await _count(session, TaskRow) == 1
        assert await _count(session, PlanRow) == 1
        assert await _count(session, MergeRequestRow) == 1
        assert await _count(session, AnalystConversationStepRow) == 1
        assert await _count(session, AnalystConversationFragmentRow) == 1
        surviving = (await session.execute(select(TaskRow.external_id))).scalar_one()
        assert surviving == "DM-2"


@pytest.mark.asyncio
async def test_reset_unknown_ticket_is_a_noop(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        summary = await reset_ticket_state(session, tracker="jira", external_id="DM-404")
    assert summary.found is False
    assert summary.total == 0


@pytest.mark.asyncio
async def test_reset_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as session:
        await _seed(session, "DM-1", iid=1)

    async with session_scope(session_factory) as session:
        first = await reset_ticket_state(session, tracker="jira", external_id="DM-1")
    async with session_scope(session_factory) as session:
        second = await reset_ticket_state(session, tracker="jira", external_id="DM-1")

    assert first.found is True
    assert second.found is False
    assert second.total == 0


@pytest.mark.asyncio
async def test_reset_leaves_marker_for_late_writers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The reset stamps ticket_resets; was_reset_since compares against a
    run's start time so a run in flight during the reset discards its
    late writes, while runs started after proceed normally."""
    from datetime import datetime, timedelta, timezone

    from virtual_dev.application.services.ticket_reset import was_reset_since

    async with session_scope(session_factory) as session:
        await _seed(session, "DM-1", 1)

    before_reset = datetime.now(timezone.utc) - timedelta(seconds=1)
    async with session_scope(session_factory) as session:
        await reset_ticket_state(session, tracker="jira", external_id="DM-1")

    async with session_factory() as session:
        # A run that started BEFORE the reset must discard.
        assert await was_reset_since(
            session, tracker="jira", external_id="DM-1", since=before_reset,
        )
        # A run started after the reset proceeds.
        after = datetime.now(timezone.utc) + timedelta(seconds=1)
        assert not await was_reset_since(
            session, tracker="jira", external_id="DM-1", since=after,
        )
        # Unknown ticket → no marker.
        assert not await was_reset_since(
            session, tracker="jira", external_id="DM-999", since=before_reset,
        )


@pytest.mark.asyncio
async def test_reset_purges_pending_bus_messages_for_the_ticket(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from virtual_dev.infrastructure.db.models import AgentMessageRow

    async with session_scope(session_factory) as session:
        await _seed(session, "DM-1", 1)
        session.add_all([
            AgentMessageRow(
                external_id="m1", from_agent="orch", to_agent="analyst",
                topic="task.discovered",
                payload_json={"tracker": "jira", "external_id": "DM-1"},
            ),
            AgentMessageRow(
                external_id="m2", from_agent="orch", to_agent="analyst",
                topic="task.discovered",
                payload_json={"tracker": "jira", "external_id": "DM-2"},
            ),
        ])

    async with session_scope(session_factory) as session:
        summary = await reset_ticket_state(
            session, tracker="jira", external_id="DM-1",
        )
    assert summary.bus_messages == 1

    async with session_factory() as session:
        left = (await session.execute(
            select(AgentMessageRow.external_id)
        )).scalars().all()
    assert list(left) == ["m2"]
