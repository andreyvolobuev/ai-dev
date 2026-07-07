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
