"""Tests for AnalystInbox + the analyst's resumable session (Phase 5.0)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents.analyst import (
    AnalystEffect,
    AnalystRunInput,
    AnalystRunResult,
)
from virtual_dev.application.services.analyst_session_repo import (
    AnalystSessionRepository,
)
from virtual_dev.application.services.communicator import CommunicatorService
from virtual_dev.application.services.injection_filter import InjectionFilter
from virtual_dev.domain.models.chat import ChatMessage, ChatUser
from virtual_dev.domain.models.plan import Plan, PlanStatus
from virtual_dev.domain.models.task import TaskStatus
from virtual_dev.domain.ports.chat import ChatPort
from virtual_dev.domain.ports.message_bus import AgentMessage
from virtual_dev.infrastructure.config import (
    AgentsCfg,
    AppConfig,
    ClarificationCfg,
    EscalationCfg,
    MappingsCfg,
    MmTemplatesCfg,
    NotificationsCfg,
    RepositoryCfg,
)
from virtual_dev.infrastructure.db import PlanRow, TaskRow
from virtual_dev.infrastructure.db.base import session_scope
from virtual_dev.runtime.workers.analyst_inbox import AnalystInbox


# ============================================================
#                          Fakes
# ============================================================


class _FakeChat(ChatPort):
    def __init__(self, *, users: dict[str, str] | None = None) -> None:
        self._users = users or {}
        self.sent_dms: list[tuple[str, str]] = []

    async def read_thread(self, thread_root_id: str) -> Sequence[ChatMessage]:
        return []

    async def send_direct(self, user_id: str, text: str) -> ChatMessage:
        self.sent_dms.append((user_id, text))
        return ChatMessage(
            id=f"bot-{len(self.sent_dms)}", channel_id=f"dm-{user_id}",
            author_id="uid-bot", text=text,
            timestamp=datetime.now(timezone.utc), trusted=True,
        )

    async def send_to_channel(
        self, channel_id: str, text: str, thread_root_id: str | None = None,
    ) -> ChatMessage:
        return ChatMessage(
            id="x", channel_id=channel_id, author_id="uid-bot",
            text=text, timestamp=datetime.now(timezone.utc), trusted=True,
        )

    async def find_user_by_email(self, email: str) -> ChatUser | None:
        return None

    async def find_user_by_username(self, username: str) -> ChatUser | None:
        uid = self._users.get(username)
        return ChatUser(id=uid, username=username) if uid else None

    async def add_reaction(self, post_id: str, emoji_name: str) -> None:
        pass

    async def get_post(self, post_id: str) -> ChatMessage | None:
        return None

    async def read_channel_since(
        self, channel_id: str, since: datetime,
    ) -> list[ChatMessage]:
        return []

    def subscribe(self) -> AsyncIterator[ChatMessage]:  # pragma: no cover
        async def _gen() -> AsyncIterator[ChatMessage]:
            if False:
                yield  # type: ignore[unreachable]
        return _gen()


class _ScriptedAnalyst:
    """Drop-in for AnalystAgent: returns scripted run results."""

    def __init__(self, runs: list[AnalystRunResult]) -> None:
        self._runs = list(runs)
        self.calls: list[AnalystRunInput] = []

    async def run(self, inp: AnalystRunInput) -> AnalystRunResult:
        self.calls.append(inp)
        if not self._runs:
            raise AssertionError("analyst called more times than scripted")
        return self._runs.pop(0)

    async def load_task(self, tracker: str, external_id: str) -> TaskRow | None:
        # Tests seed the row themselves; just resolve via the session.
        from sqlalchemy import select

        async with self._sf() as session:
            return (await session.execute(
                select(TaskRow).where(
                    TaskRow.tracker == tracker,
                    TaskRow.external_id == external_id,
                )
            )).scalar_one_or_none()

    def attach_session_factory(self, sf: async_sessionmaker[AsyncSession]) -> None:
        self._sf = sf

    async def has_fresh_plan(self, task_row: TaskRow) -> bool:
        return False

    async def save_plan(self, plan: Plan) -> None:
        from virtual_dev.infrastructure.db.mappers import plan_to_row

        async with session_scope(self._sf) as session:
            session.add(plan_to_row(plan))


# ============================================================
#                          Helpers
# ============================================================


def _config() -> AppConfig:
    return AppConfig(
        repositories=[RepositoryCfg(key="x", url="git@x:x.git")],
        agents=AgentsCfg(
            escalation=EscalationCfg(mattermost_user="lead"),
            clarification=ClarificationCfg(
                coalesce_window_seconds=1,
                max_planner_calls_per_goal=8,
                max_goal_age_hours=48,
            ),
        ),
        mappings=MappingsCfg(),
        notifications=NotificationsCfg(mattermost=MmTemplatesCfg()),
    )


async def _seed_task_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> TaskRow:
    async with session_scope(session_factory) as session:
        row = TaskRow(
            tracker="jira", external_id="DM-42",
            title="t", description="нужен пример body",
            url="https://jira/DM-42",
            priority="medium", external_status="To Do",
            internal_status=TaskStatus.DISCOVERED.value,
            reporter_id="you",
        )
        session.add(row)
        await session.flush()
        return row


def _make_inbox(
    session_factory: async_sessionmaker[AsyncSession],
    chat: ChatPort,
    analyst: _ScriptedAnalyst,
) -> AnalystInbox:
    analyst.attach_session_factory(session_factory)
    return AnalystInbox(
        analyst=analyst,                              # type: ignore[arg-type]
        session_repo=AnalystSessionRepository(session_factory),
        communicator=CommunicatorService(
            chat, InjectionFilter(), respect_working_hours=False,
        ),
        task_tracker=None,
        config=_config(),
        message_bus=None,
        post_to_tracker=False,
        session_factory=session_factory,
    )


def _msg(external_id: str = "DM-42") -> AgentMessage:
    return AgentMessage(
        id="m1", from_agent="orchestrator", to_agent="analyst",
        topic="task.discovered",
        payload={"tracker": "jira", "external_id": external_id},
    )


def _ready_plan() -> Plan:
    return Plan(
        task_external_id="DM-42", tracker="jira",
        summary="ok", steps=[], open_questions=[], risks=[],
        confidence=0.9, status=PlanStatus.READY,
        target_repo_key="x",
    )


# ============================================================
#                          Tests
# ============================================================


@pytest.mark.asyncio
async def test_inbox_runs_analyst_and_finalises_plan(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Analyst submits a ready plan on first run → task ready, plan saved."""
    await _seed_task_row(session_factory)
    chat = _FakeChat()
    analyst = _ScriptedAnalyst([
        AnalystRunResult(
            effects=[AnalystEffect(
                kind="plan_submitted",
                payload={"summary": "ok", "status": "ready", "target_repo_key": "x"},
            )],
            cost_usd=0.01, turns=2, stopped_reason="end_turn",
            plan=_ready_plan(),
        ),
    ])
    inbox = _make_inbox(session_factory, chat, analyst)
    await inbox.handle(_msg())

    from sqlalchemy import select

    async with session_factory() as session:
        task_row = (await session.execute(
            select(TaskRow).where(TaskRow.external_id == "DM-42")
        )).scalar_one()
        plans = list((await session.execute(
            select(PlanRow).where(PlanRow.task_external_id == "DM-42")
        )).scalars().all())

    assert task_row.internal_status == TaskStatus.READY.value
    assert len(plans) == 1
    assert plans[0].status == PlanStatus.READY.value


@pytest.mark.asyncio
async def test_inbox_installs_awaiting_on_ask(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Analyst dispatches an ASK → task gets awaiting_* fields, no plan yet."""
    await _seed_task_row(session_factory)
    chat = _FakeChat()
    analyst = _ScriptedAnalyst([
        AnalystRunResult(
            effects=[AnalystEffect(
                kind="ask_dispatched",
                payload={
                    "asked_post_id": "post-1",
                    "channel_id": "dm-uid-alice",
                    "target_user_id": "uid-alice",
                    "target_username": "alice",
                    "asked_text": "вопрос?",
                    "dedupe_key": None,
                },
            )],
            cost_usd=0.005, turns=1, stopped_reason="end_turn",
            plan=None,
        ),
    ])
    inbox = _make_inbox(session_factory, chat, analyst)
    await inbox.handle(_msg())

    from sqlalchemy import select

    async with session_factory() as session:
        task_row = (await session.execute(
            select(TaskRow).where(TaskRow.external_id == "DM-42")
        )).scalar_one()
    assert task_row.awaiting_post_id == "post-1"
    assert task_row.awaiting_user_id == "uid-alice"
    assert task_row.awaiting_username == "alice"
    assert task_row.internal_status == TaskStatus.PLANNING.value


@pytest.mark.asyncio
async def test_inbox_resumes_analyst_after_coalesced_reply(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Coalesced reply re-invokes the analyst; second run sees the
    HUMAN_REPLIED step in its history."""
    await _seed_task_row(session_factory)
    chat = _FakeChat()
    analyst = _ScriptedAnalyst([
        AnalystRunResult(
            effects=[AnalystEffect(
                kind="ask_dispatched",
                payload={
                    "asked_post_id": "post-1",
                    "channel_id": "dm-uid-alice",
                    "target_user_id": "uid-alice",
                    "target_username": "alice",
                    "asked_text": "?",
                    "dedupe_key": None,
                },
            )],
            cost_usd=0.0, turns=1, stopped_reason="end_turn",
            plan=None,
        ),
        AnalystRunResult(
            effects=[AnalystEffect(
                kind="plan_submitted",
                payload={"summary": "got it", "status": "ready", "target_repo_key": "x"},
            )],
            cost_usd=0.0, turns=1, stopped_reason="end_turn",
            plan=_ready_plan(),
        ),
    ])
    inbox = _make_inbox(session_factory, chat, analyst)
    await inbox.handle(_msg())

    # Now simulate alice replying.
    from sqlalchemy import select

    async with session_factory() as session:
        task_row = (await session.execute(
            select(TaskRow).where(TaskRow.external_id == "DM-42")
        )).scalar_one()
    reply = ChatMessage(
        id="reply-1", channel_id="dm-uid-alice", author_id="uid-alice",
        text="POST /tasks {x:1}",
        timestamp=datetime.now(timezone.utc),
        thread_root_id="post-1", trusted=False,
    )
    await inbox.append_fragment(task_row.id, reply)

    # Force coalesce window past.
    async with session_scope(session_factory) as session:
        row = (await session.execute(
            select(TaskRow).where(TaskRow.id == task_row.id)
        )).scalar_one()
        row.last_fragment_at = datetime.now(timezone.utc) - timedelta(seconds=5)

    await inbox.flush_idle()

    # Second analyst run had history with the HUMAN_REPLIED step.
    assert len(analyst.calls) == 2
    second_history_kinds = {s.kind.value for s in analyst.calls[1].history}
    assert "human_replied" in second_history_kinds

    # The orchestrator acknowledged receipt with a short DM to the
    # awaitee (alice). It must NOT install awaiting state — the bot
    # is not waiting for a reply to "Спасибо".
    from virtual_dev.runtime.workers.analyst_inbox import _ACK_PHRASES

    ack_dms = [d for d in chat.sent_dms if d[0] == "uid-alice" and d[1] in _ACK_PHRASES]
    assert ack_dms, (
        f"expected one ack DM to uid-alice; got {chat.sent_dms!r}"
    )

    async with session_factory() as session:
        refreshed = (await session.execute(
            select(TaskRow).where(TaskRow.external_id == "DM-42")
        )).scalar_one()
    assert refreshed.internal_status == TaskStatus.READY.value


@pytest.mark.asyncio
async def test_inbox_escalates_when_run_produces_no_effect(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Silent analyst (max_turns hit, no terminal effect) must not
    leave the task hanging — orchestrator escalates."""
    await _seed_task_row(session_factory)
    chat = _FakeChat(users={"lead": "uid-lead"})
    analyst = _ScriptedAnalyst([
        AnalystRunResult(
            effects=[],   # no effects
            cost_usd=0.0, turns=30, stopped_reason="max_turns",
            plan=None,
        ),
    ])
    inbox = _make_inbox(session_factory, chat, analyst)
    await inbox.handle(_msg())

    from sqlalchemy import select

    async with session_factory() as session:
        task_row = (await session.execute(
            select(TaskRow).where(TaskRow.external_id == "DM-42")
        )).scalar_one()
    assert task_row.internal_status == TaskStatus.FAILED.value
    # Lead got the escalation DM.
    lead_dms = [(uid, txt) for uid, txt in chat.sent_dms if uid == "uid-lead"]
    assert len(lead_dms) == 1


@pytest.mark.asyncio
async def test_inbox_routes_fragment_to_correct_task(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """find_task_by_thread / find_task_by_channel return the task with
    matching awaiting_* state."""
    chat = _FakeChat()
    repo = AnalystSessionRepository(session_factory)
    analyst = _ScriptedAnalyst([])
    inbox = _make_inbox(session_factory, chat, analyst)

    # Two tasks, only one is waiting.
    from sqlalchemy import select

    async with session_scope(session_factory) as session:
        a = TaskRow(
            tracker="jira", external_id="A", title="a", description="",
            url="", priority="medium", external_status="x",
            internal_status=TaskStatus.PLANNING.value,
            awaiting_post_id="post-A", awaiting_user_id="uid-1",
            awaiting_username="alice", awaiting_channel_id="dm-1",
        )
        b = TaskRow(
            tracker="jira", external_id="B", title="b", description="",
            url="", priority="medium", external_status="x",
            internal_status=TaskStatus.PLANNING.value,
        )
        session.add(a)
        session.add(b)
        await session.flush()

    found = await inbox.find_task_by_thread("post-A")
    assert found is not None and found.external_id == "A"

    found2 = await inbox.find_task_by_channel("dm-1", "uid-1")
    assert found2 is not None and found2.external_id == "A"

    # Non-existent post → None.
    miss = await inbox.find_task_by_thread("post-X")
    assert miss is None
