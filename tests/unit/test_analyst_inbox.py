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
        self.channel_posts: list[tuple[str, str | None, str]] = []

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
        self.channel_posts.append((channel_id, thread_root_id, text))
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


@pytest.mark.asyncio
async def test_late_fragment_during_flush_is_not_lost(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A fragment that lands between the coalesce snapshot and
    clear_awaiting (user typed one more message during the flush) must
    NOT be silently dropped: the resume is deferred and the next
    flush_idle tick delivers BOTH replies to the analyst."""
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

    from sqlalchemy import select

    async with session_factory() as session:
        task_row = (await session.execute(
            select(TaskRow).where(TaskRow.external_id == "DM-42")
        )).scalar_one()

    def _reply(post_id: str, text: str) -> ChatMessage:
        return ChatMessage(
            id=post_id, channel_id="dm-uid-alice", author_id="uid-alice",
            text=text, timestamp=datetime.now(timezone.utc),
            thread_root_id="post-1", trusted=False,
        )

    await inbox.append_fragment(task_row.id, _reply("reply-1", "first half"))

    # Simulate the race: the ✅-reaction network call is when the user's
    # second message arrives.
    original_add_reaction = chat.add_reaction

    async def _racy_add_reaction(post_id: str, emoji_name: str) -> None:
        await inbox.append_fragment(task_row.id, _reply("reply-2", "second half"))
        await original_add_reaction(post_id, emoji_name)

    chat.add_reaction = _racy_add_reaction  # type: ignore[method-assign]

    async with session_scope(session_factory) as session:
        row = (await session.execute(
            select(TaskRow).where(TaskRow.id == task_row.id)
        )).scalar_one()
        row.last_fragment_at = datetime.now(timezone.utc) - timedelta(seconds=5)

    await inbox.flush_idle()

    # Resume deferred: analyst NOT run yet, awaiting still armed.
    assert len(analyst.calls) == 1
    async with session_factory() as session:
        row = (await session.execute(
            select(TaskRow).where(TaskRow.id == task_row.id)
        )).scalar_one()
        assert row.awaiting_post_id == "post-1"

    # Next tick: window past again → late fragment coalesces, analyst runs.
    chat.add_reaction = original_add_reaction  # type: ignore[method-assign]
    async with session_scope(session_factory) as session:
        row = (await session.execute(
            select(TaskRow).where(TaskRow.id == task_row.id)
        )).scalar_one()
        row.last_fragment_at = datetime.now(timezone.utc) - timedelta(seconds=5)

    await inbox.flush_idle()

    assert len(analyst.calls) == 2
    replied = [s for s in analyst.calls[1].history if s.kind.value == "human_replied"]
    texts = " || ".join(s.text or "" for s in replied)
    assert "first half" in texts and "second half" in texts


@pytest.mark.asyncio
async def test_run_results_discarded_when_ticket_reset_mid_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """/reset landing while the analyst is thinking: the finished run's
    plan must NOT be saved — save_plan keys on external_id and would
    resurrect the wiped ticket."""
    from sqlalchemy import select as sa_select

    from virtual_dev.application.services.ticket_reset import reset_ticket_state
    from virtual_dev.infrastructure.db import PlanRow

    await _seed_task_row(session_factory)
    chat = _FakeChat()

    class _ResetMidRunAnalyst(_ScriptedAnalyst):
        async def run(self, inp: AnalystRunInput) -> AnalystRunResult:
            async with session_scope(self._sf) as session:
                await reset_ticket_state(
                    session, tracker="jira", external_id="DM-42",
                )
            return await super().run(inp)

    analyst = _ResetMidRunAnalyst([
        AnalystRunResult(
            effects=[AnalystEffect(
                kind="plan_submitted",
                payload={"summary": "late", "status": "ready", "target_repo_key": "x"},
            )],
            cost_usd=0.0, turns=1, stopped_reason="end_turn",
            plan=_ready_plan(),
        ),
    ])
    inbox = _make_inbox(session_factory, chat, analyst)
    await inbox.handle(_msg())

    async with session_factory() as session:
        plans = (await session.execute(
            sa_select(PlanRow).where(PlanRow.task_external_id == "DM-42")
        )).scalars().all()
    assert plans == []


class _TeamThreadChat(_FakeChat):
    def __init__(
        self, thread: list[ChatMessage], usernames: dict[str, str],
        full_names: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        super().__init__()
        self._thread = thread
        self._usernames = usernames
        self._full_names = full_names or {}

    async def read_thread(self, thread_root_id: str) -> Sequence[ChatMessage]:
        return self._thread

    async def get_user_by_id(self, user_id: str) -> ChatUser | None:
        name = self._usernames.get(user_id)
        first, last = self._full_names.get(user_id, (None, None))
        return ChatUser(id=user_id, username=name, first_name=first, last_name=last) if name else None


@pytest.mark.asyncio
async def test_team_thread_replies_reach_analyst_with_authors(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Several teammates answer in the team thread; the analyst gets one
    HUMAN_REPLIED step marked as the team's, each reply signed by its author."""
    await _seed_task_row(session_factory)
    now = datetime.now(timezone.utc)
    replies = [
        ChatMessage(
            id="r-1", channel_id="ch-team", author_id="uid-a", text="ок, но без миграции",
            timestamp=now, thread_root_id="root-1",
        ),
        ChatMessage(
            id="r-2", channel_id="ch-team", author_id="uid-b", text="согласен с Сашей",
            timestamp=now, thread_root_id="root-1",
        ),
    ]
    chat = _TeamThreadChat(replies, {"uid-a": "a.ivanova", "uid-b": "b.petrov"})
    analyst = _ScriptedAnalyst([
        AnalystRunResult(
            effects=[AnalystEffect(
                kind="ask_dispatched",
                payload={
                    "audience": "team", "asked_post_id": "root-1",
                    "channel_id": "ch-team", "target_user_id": "",
                    "target_username": None, "asked_text": "предлагаю X",
                    "dedupe_key": None,
                },
            )],
            cost_usd=0.0, turns=1, stopped_reason="end_turn", plan=None,
        ),
        AnalystRunResult(
            effects=[AnalystEffect(
                kind="plan_submitted",
                payload={"summary": "ok", "status": "ready", "target_repo_key": "x"},
            )],
            cost_usd=0.0, turns=1, stopped_reason="end_turn", plan=_ready_plan(),
        ),
    ])
    inbox = _make_inbox(session_factory, chat, analyst)
    inbox._config.notifications.mattermost.plan_ready_in_thread = "план готов"
    await inbox.handle(_msg())

    task_row = await inbox.find_task_by_thread("root-1")
    assert task_row is not None
    for reply in replies:
        await inbox.append_fragment(task_row.id, reply)
    async with session_scope(session_factory) as session:
        from sqlalchemy import select

        row = (await session.execute(
            select(TaskRow).where(TaskRow.id == task_row.id)
        )).scalar_one()
        row.last_fragment_at = datetime.now(timezone.utc) - timedelta(seconds=5)

    await inbox.flush_idle()

    reply_step = [
        s for s in analyst.calls[1].history if s.kind.value == "human_replied"
    ][0]
    assert reply_step.metadata["audience"] == "team"
    assert reply_step.text == (
        "@a.ivanova: ок, но без миграции\n\n@b.petrov: согласен с Сашей"
    )
    asked_step = [s for s in analyst.calls[1].history if s.kind.value == "bot_asked"][0]
    assert asked_step.metadata["audience"] == "team"
    # The thread that agreed the plan hears that it's fixed.
    assert chat.channel_posts == [("ch-team", "root-1", "план готов")]


@pytest.mark.asyncio
async def test_team_reply_written_mid_run_is_not_lost(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A reply posted while the analyst ran isn't buffered (nobody waits
    on the thread then). The next coalesce reads the thread and delivers
    it together with the new reply — but not what was already shown."""
    await _seed_task_row(session_factory)
    now = datetime.now(timezone.utc)

    def post(post_id: str, author: str, text: str) -> ChatMessage:
        return ChatMessage(
            id=post_id, channel_id="ch-team", author_id=author, text=text,
            timestamp=now, thread_root_id="root-1",
        )

    seen_mid_run = post("r-1", "uid-a", "ок")
    missed = post("r-2", "uid-b", "а ещё алерт")
    buffered = post("r-3", "uid-a", "согласна")
    chat = _TeamThreadChat(
        [seen_mid_run, missed, buffered], {"uid-a": "a.ivanova", "uid-b": "b.petrov"},
    )
    ask = AnalystEffect(kind="ask_dispatched", payload={
        "audience": "team", "asked_post_id": "root-1", "channel_id": "ch-team",
        "target_user_id": "", "target_username": None, "asked_text": "итог X",
        "dedupe_key": None,
    })
    analyst = _ScriptedAnalyst([
        AnalystRunResult(
            effects=[
                AnalystEffect(kind="team_replies_seen",
                              payload={"post_ids": ["r-1"], "text": "@a.ivanova: ок"}),
                ask,
            ],
            cost_usd=0.0, turns=1, stopped_reason="end_turn", plan=None,
        ),
        AnalystRunResult(effects=[ask], cost_usd=0.0, turns=1,
                         stopped_reason="end_turn", plan=None),
    ])
    inbox = _make_inbox(session_factory, chat, analyst)
    await inbox.handle(_msg())

    task_row = await inbox.find_task_by_thread("root-1")
    assert task_row is not None
    await inbox.append_fragment(task_row.id, buffered)
    async with session_scope(session_factory) as session:
        from sqlalchemy import select

        row = (await session.execute(
            select(TaskRow).where(TaskRow.id == task_row.id)
        )).scalar_one()
        row.last_fragment_at = datetime.now(timezone.utc) - timedelta(seconds=5)

    await inbox.flush_idle()

    replies = [s for s in analyst.calls[1].history if s.kind.value == "human_replied"]
    assert [s.text for s in replies] == [
        "@a.ivanova: ок",
        "@b.petrov: а ещё алерт\n\n@a.ivanova: согласна",
    ]


@pytest.mark.asyncio
async def test_team_thread_still_heard_while_analyst_awaits_a_dm(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """DM-3784: the analyst nudged the reporter by DM, the reporter
    answered in the team thread instead. That reply must still reach the
    analyst — not fall through to intake as an unrelated mention."""
    await _seed_task_row(session_factory)
    now = datetime.now(timezone.utc)
    first = ChatMessage(
        id="r-1", channel_id="ch-team", author_id="uid-g", text="мне ок, ждём ещё двоих",
        timestamp=now, thread_root_id="root-1",
    )
    approval = ChatMessage(
        id="r-2", channel_id="ch-team", author_id="uid-k", text="ок, но тип TEXT",
        timestamp=now + timedelta(seconds=1), thread_root_id="root-1",
    )
    thread = [first]
    chat = _TeamThreadChat(
        thread, {"uid-g": "g.golovin", "uid-k": "d.kozhevnikov"},
        {"uid-g": ("Глеб", "Головин"), "uid-k": ("Дмитрий", "Кожевников")},
    )
    team_ask = AnalystEffect(kind="ask_dispatched", payload={
        "audience": "team", "asked_post_id": "root-1", "channel_id": "ch-team",
        "target_user_id": "", "target_username": None, "asked_text": "предлагаю X",
        "dedupe_key": None,
    })
    dm_nudge = AnalystEffect(kind="ask_dispatched", payload={
        "asked_post_id": "dm-1", "channel_id": "dm-uid-k", "target_user_id": "uid-k",
        "target_username": "d.kozhevnikov", "asked_text": "глянешь тред?",
        "dedupe_key": None,
    })
    analyst = _ScriptedAnalyst([
        AnalystRunResult(effects=[team_ask], cost_usd=0.0, turns=1,
                         stopped_reason="end_turn", plan=None),
        AnalystRunResult(effects=[dm_nudge], cost_usd=0.0, turns=1,
                         stopped_reason="end_turn", plan=None),
        AnalystRunResult(effects=[team_ask], cost_usd=0.0, turns=1,
                         stopped_reason="end_turn", plan=None),
    ])
    inbox = _make_inbox(session_factory, chat, analyst)
    await inbox.handle(_msg())

    async def reply_in_thread(post: ChatMessage) -> TaskRow:
        task_row = await inbox.find_task_by_thread("root-1")
        assert task_row is not None
        await inbox.append_fragment(task_row.id, post)
        async with session_scope(session_factory) as session:
            from sqlalchemy import select

            row = (await session.execute(
                select(TaskRow).where(TaskRow.id == task_row.id)
            )).scalar_one()
            row.last_fragment_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        await inbox.flush_idle()
        return task_row

    await reply_in_thread(first)
    thread.append(approval)
    task_row = await reply_in_thread(approval)

    assert task_row.awaiting_post_id == "dm-1"
    assert len(analyst.calls) == 3
    last = [s for s in analyst.calls[2].history if s.kind.value == "human_replied"][-1]
    # Real names from the directory: the analyst must not guess «Гоша» from @g.golovin.
    assert (last.text, last.metadata["audience"]) == (
        "@d.kozhevnikov (Дмитрий Кожевников): ок, но тип TEXT", "team",
    )


@pytest.mark.asyncio
async def test_team_thread_of_finished_task_is_not_claimed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Once the plan is in, the thread is ordinary chat again."""
    await _seed_task_row(session_factory)
    analyst = _ScriptedAnalyst([
        AnalystRunResult(
            effects=[AnalystEffect(kind="ask_dispatched", payload={
                "audience": "team", "asked_post_id": "root-1", "channel_id": "ch-team",
                "target_user_id": "", "target_username": None, "asked_text": "X?",
                "dedupe_key": None,
            })],
            cost_usd=0.0, turns=1, stopped_reason="end_turn", plan=None,
        ),
    ])
    inbox = _make_inbox(session_factory, _FakeChat(), analyst)
    await inbox.handle(_msg())
    async with session_scope(session_factory) as session:
        from sqlalchemy import select

        row = (await session.execute(select(TaskRow))).scalar_one()
        row.awaiting_post_id = None
        row.internal_status = TaskStatus.READY.value

    assert await inbox.find_task_by_thread("root-1") is None


def _team_ask() -> AnalystRunResult:
    return AnalystRunResult(
        effects=[AnalystEffect(kind="ask_dispatched", payload={
            "audience": "team", "asked_post_id": "root-1", "channel_id": "ch-team",
            "target_user_id": "", "target_username": None, "asked_text": "X?",
            "dedupe_key": None,
        })],
        cost_usd=0.0, turns=1, stopped_reason="end_turn", plan=None,
    )


async def _load_row(session_factory: async_sessionmaker[AsyncSession]) -> TaskRow:
    from sqlalchemy import select

    async with session_factory() as session:
        return (await session.execute(select(TaskRow))).scalar_one()


@pytest.mark.asyncio
async def test_silence_on_team_proposal_is_consent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«@vasya, перепишу ручку /data» — nobody objected, so the analyst
    goes ahead and tells the thread, instead of waiting days for an «ок»."""
    await _seed_task_row(session_factory)
    chat = _FakeChat(users={"lead": "uid-lead"})
    plan_run = AnalystRunResult(
        effects=[AnalystEffect(kind="plan_submitted", payload={
            "summary": "ok", "status": "ready", "target_repo_key": "x",
            "thread_note": "Возражений нет — взялась",
        })],
        cost_usd=0.0, turns=1, stopped_reason="end_turn", plan=_ready_plan(),
    )
    analyst = _ScriptedAnalyst([_team_ask(), plan_run])
    inbox = _make_inbox(session_factory, chat, analyst)
    await inbox.handle(_msg())

    row = await _load_row(session_factory)
    # The consent clock runs in working hours, not 48h of wall time.
    assert row.analyst_deadline_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc) + timedelta(days=4)
    async with session_scope(session_factory) as session:
        from sqlalchemy import select

        row = (await session.execute(select(TaskRow))).scalar_one()
        row.analyst_deadline_at = datetime.now(timezone.utc) - timedelta(minutes=1)

    await inbox.sweep_deadlines()

    assert len(analyst.calls) == 2
    assert analyst.calls[1].history[-1].metadata.get("silence_consent") is True
    row = await _load_row(session_factory)
    assert row.internal_status == TaskStatus.READY.value
    assert chat.sent_dms == []
    assert chat.channel_posts == [("ch-team", "root-1", "Возражений нет — взялась")]


@pytest.mark.asyncio
async def test_overdue_dm_ask_pings_lead_and_keeps_listening(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A question to one person is a fact we need, not a proposal — silence
    there is not consent: ping the lead and keep waiting."""
    await _seed_task_row(session_factory)
    chat = _FakeChat(users={"lead": "uid-lead"})
    dm_ask = AnalystRunResult(
        effects=[AnalystEffect(kind="ask_dispatched", payload={
            "audience": "dm", "asked_post_id": "dm-post", "channel_id": "dm-ch",
            "target_user_id": "uid-v", "target_username": "v.v", "asked_text": "body?",
            "dedupe_key": None,
        })],
        cost_usd=0.0, turns=1, stopped_reason="end_turn", plan=None,
    )
    inbox = _make_inbox(session_factory, chat, _ScriptedAnalyst([dm_ask]))
    await inbox.handle(_msg())
    async with session_scope(session_factory) as session:
        from sqlalchemy import select

        row = (await session.execute(select(TaskRow))).scalar_one()
        row.analyst_deadline_at = datetime.now(timezone.utc) - timedelta(minutes=1)

    await inbox.sweep_deadlines()

    row = await _load_row(session_factory)
    assert row.internal_status != TaskStatus.FAILED.value
    assert row.awaiting_post_id == "dm-post"
    assert row.analyst_deadline_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc) + timedelta(hours=47)
    assert [uid for uid, _ in chat.sent_dms] == ["uid-lead"]


@pytest.mark.asyncio
async def test_stuck_task_is_revived_by_team_reply(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_task_row(session_factory)
    reply = ChatMessage(
        id="r-1", channel_id="ch-team", author_id="uid-a", text="ок",
        timestamp=datetime.now(timezone.utc), thread_root_id="root-1",
    )
    chat = _TeamThreadChat([reply], {"uid-a": "a.gulvanyuk"})
    analyst = _ScriptedAnalyst([_team_ask(), _team_ask()])
    inbox = _make_inbox(session_factory, chat, analyst)
    await inbox.handle(_msg())
    await inbox._terminate(await _load_row(session_factory), kind="stuck", reason="max_iterations_reached")

    task_row = await inbox.find_task_by_thread("root-1")
    assert task_row is not None
    await inbox.append_fragment(task_row.id, reply)
    async with session_scope(session_factory) as session:
        from sqlalchemy import select

        row = (await session.execute(select(TaskRow))).scalar_one()
        row.last_fragment_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    await inbox.flush_idle()

    assert len(analyst.calls) == 2
    assert analyst.calls[1].history[-1].text == "@a.gulvanyuk: ок"
    row = await _load_row(session_factory)
    assert row.internal_status == TaskStatus.CLARIFYING.value
    assert row.analyst_iteration_count == 1


@pytest.mark.asyncio
async def test_blocked_task_is_not_revived_by_thread(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_task_row(session_factory)
    inbox = _make_inbox(session_factory, _FakeChat(), _ScriptedAnalyst([_team_ask()]))
    await inbox.handle(_msg())
    await inbox._terminate(await _load_row(session_factory), kind="blocked", reason="contradiction")

    assert await inbox.find_task_by_thread("root-1") is None
