"""QuestionRepository — round-trip + idempotent fragment append + queries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.services.clarification.repo import QuestionRepository
from virtual_dev.domain.models.clarification import (
    Classification,
    QuestionState,
    Stakeholder,
    StakeholderKind,
)


def _stakeholder() -> Stakeholder:
    return Stakeholder(
        kind=StakeholderKind.EXPLICIT_HANDLE,
        raw_hint="alice",
        resolved_mm_user_id="uid-alice",
        display_name="alice",
    )


@pytest.mark.asyncio
async def test_create_root_assigns_root_id_to_self(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = QuestionRepository(session_factory=session_factory)
    deadline = datetime.now(timezone.utc) + timedelta(hours=48)

    q = await repo.create_root(
        tracker="jira", task_external_id="DM-1", plan_id=42,
        text="Как называется ручка?", why_it_matters="без неё код не написать",
        stakeholder=_stakeholder(),
        coalesce_window_seconds=600, deadline_at=deadline,
    )
    assert q.id == q.root_id
    assert q.parent_id is None
    assert q.chain_depth == 0
    assert q.state is QuestionState.PENDING


@pytest.mark.asyncio
async def test_create_child_inherits_root_and_increments_depth(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = QuestionRepository(session_factory=session_factory)
    deadline = datetime.now(timezone.utc) + timedelta(hours=48)
    parent = await repo.create_root(
        tracker="jira", task_external_id="DM-1", plan_id=42,
        text="Q", why_it_matters="",
        stakeholder=_stakeholder(),
        coalesce_window_seconds=600, deadline_at=deadline,
    )

    child = await repo.create_child(
        parent=parent,
        text="Q", why_it_matters="",
        stakeholder=Stakeholder(
            kind=StakeholderKind.EXPLICIT_HANDLE, raw_hint="bob",
            resolved_mm_user_id="uid-bob",
        ),
        coalesce_window_seconds=600, deadline_at=deadline,
    )
    assert child.parent_id == parent.id
    assert child.root_id == parent.root_id
    assert child.chain_depth == 1


@pytest.mark.asyncio
async def test_append_fragment_is_idempotent_on_post_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = QuestionRepository(session_factory=session_factory)
    deadline = datetime.now(timezone.utc) + timedelta(hours=48)
    q = await repo.create_root(
        tracker="jira", task_external_id="DM-1", plan_id=1,
        text="Q", why_it_matters="",
        stakeholder=_stakeholder(),
        coalesce_window_seconds=600, deadline_at=deadline,
    )
    now = datetime.now(timezone.utc)

    assert await repo.append_fragment(
        question_id=q.id, mm_post_id="post-1", text="первый кусок", received_at=now,
    ) is True
    assert await repo.append_fragment(
        question_id=q.id, mm_post_id="post-1", text="первый кусок (replay)",
        received_at=now,
    ) is False  # duplicate
    assert await repo.append_fragment(
        question_id=q.id, mm_post_id="post-2", text="второй", received_at=now,
    ) is True

    fragments = await repo.list_unflushed_fragments(q.id)
    assert [f.mm_post_id for f in fragments] == ["post-1", "post-2"]
    assert [f.text for f in fragments] == ["первый кусок", "второй"]


@pytest.mark.asyncio
async def test_find_idle_coalescing_respects_window(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = QuestionRepository(session_factory=session_factory)
    deadline = datetime.now(timezone.utc) + timedelta(hours=48)
    q = await repo.create_root(
        tracker="jira", task_external_id="DM-1", plan_id=1,
        text="Q", why_it_matters="",
        stakeholder=_stakeholder(),
        coalesce_window_seconds=300, deadline_at=deadline,
    )
    long_ago = datetime.now(timezone.utc) - timedelta(seconds=600)
    await repo.append_fragment(
        question_id=q.id, mm_post_id="post-1", text="t", received_at=long_ago,
    )
    # State is now COALESCING (set by append_fragment).
    idle = await repo.find_idle_coalescing(now=datetime.now(timezone.utc))
    assert [x.id for x in idle] == [q.id]

    # If we ask "what was idle 30 minutes ago?", nothing should match
    # — last_fragment was at long_ago which is exactly the start of
    # the 600s window from now-300s.
    earlier = long_ago + timedelta(seconds=10)
    idle_earlier = await repo.find_idle_coalescing(now=earlier)
    assert idle_earlier == []


@pytest.mark.asyncio
async def test_find_overdue_picks_active_with_passed_deadline(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = QuestionRepository(session_factory=session_factory)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    q = await repo.create_root(
        tracker="jira", task_external_id="DM-1", plan_id=1,
        text="Q", why_it_matters="",
        stakeholder=_stakeholder(),
        coalesce_window_seconds=600, deadline_at=past,
    )
    await repo.update_state(q.id, QuestionState.ASKING)

    overdue = await repo.find_overdue(now=datetime.now(timezone.utc))
    assert [x.id for x in overdue] == [q.id]


@pytest.mark.asyncio
async def test_chain_user_ids_walks_ancestors(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """For cycle detection: chain_user_ids must include all ancestors."""
    repo = QuestionRepository(session_factory=session_factory)
    deadline = datetime.now(timezone.utc) + timedelta(hours=48)
    root = await repo.create_root(
        tracker="jira", task_external_id="DM-1", plan_id=1,
        text="Q", why_it_matters="",
        stakeholder=Stakeholder(
            kind=StakeholderKind.EXPLICIT_HANDLE, raw_hint="alice",
            resolved_mm_user_id="uid-alice",
        ),
        coalesce_window_seconds=600, deadline_at=deadline,
    )
    child = await repo.create_child(
        parent=root, text="Q", why_it_matters="",
        stakeholder=Stakeholder(
            kind=StakeholderKind.EXPLICIT_HANDLE, raw_hint="bob",
            resolved_mm_user_id="uid-bob",
        ),
        coalesce_window_seconds=600, deadline_at=deadline,
    )
    grand = await repo.create_child(
        parent=child, text="Q", why_it_matters="",
        stakeholder=Stakeholder(
            kind=StakeholderKind.EXPLICIT_HANDLE, raw_hint="carol",
            resolved_mm_user_id="uid-carol",
        ),
        coalesce_window_seconds=600, deadline_at=deadline,
    )
    chain = await repo.chain_user_ids(grand)
    assert chain == {"uid-alice", "uid-bob", "uid-carol"}


@pytest.mark.asyncio
async def test_save_answer_round_trip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = QuestionRepository(session_factory=session_factory)
    deadline = datetime.now(timezone.utc) + timedelta(hours=48)
    q = await repo.create_root(
        tracker="jira", task_external_id="DM-1", plan_id=1,
        text="Q", why_it_matters="",
        stakeholder=_stakeholder(),
        coalesce_window_seconds=600, deadline_at=deadline,
    )
    await repo.save_answer(
        question_id=q.id, coalesced_text="UserAPI",
        classification=Classification.DIRECT,
        extracted={"direct_answer_text": "UserAPI"},
        cost_usd=0.0001,
    )
    loaded = await repo.get(q.id)
    assert loaded is not None
    assert loaded.answer is not None
    assert loaded.answer.classification is Classification.DIRECT
    assert loaded.answer.coalesced_text == "UserAPI"
    assert loaded.answer.extracted["direct_answer_text"] == "UserAPI"
