"""End-to-end goal-driven cycle (replaces the old Q-tree integration test).

Walks one ClarificationGoal through the entire happy path:

  1. Analyst hands an open question → ``request_clarifications`` creates
     a goal and runs the planner once → ASK to team-lead.
  2. MmThreadListener receives the team-lead's reply → goal fragment.
  3. flush_idle (after window) → planner is invoked again with the
     coalesced answer → it ASKs Vasya **with a freshly-composed
     message** (not a copy of the lead's question).
  4. Vasya replies with the body example → flush_idle → planner ACHIEVES.
  5. All goals on the plan terminal → ``_maybe_resettle_plan`` re-publishes
     ``task.discovered`` and folds the answer into the task description.

This test stitches MmThreadListener + GoalOrchestrator + GoalRepository
together with stub agents, verifying they cooperate as designed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.services.clarification import (
    GoalOrchestrator,
    GoalRepository,
)
from virtual_dev.application.services.communicator import CommunicatorService
from virtual_dev.application.services.injection_filter import InjectionFilter
from virtual_dev.domain.models.chat import ChatMessage, ChatUser
from virtual_dev.domain.models.clarification_goal import (
    GoalState,
    PlannerActionKind,
    PlannerDecision,
)
from virtual_dev.domain.models.plan import OpenQuestion, Plan, PlanStatus
from virtual_dev.domain.ports.chat import ChatPort
from virtual_dev.domain.ports.message_bus import AgentMessage, MessageBusPort
from virtual_dev.infrastructure.config import (
    AgentsCfg,
    AppConfig,
    ClarificationCfg,
    EscalationCfg,
    MappingsCfg,
    MmTemplatesCfg,
    NotificationsCfg,
    RepositoryCfg,
    Settings,
)
from virtual_dev.infrastructure.db import PlanRow, TaskRow
from virtual_dev.infrastructure.db.base import session_scope
from virtual_dev.runtime.workers.mm_thread_listener import MmThreadListener

# ============================================================
#                          Fakes
# ============================================================


class _Chat(ChatPort):
    def __init__(self, users: dict[str, str]) -> None:
        self._users = users
        self.sent_dms: list[tuple[str, str]] = []
        self.reactions: list[tuple[str, str]] = []
        self._post_seq = 0

    def _next(self) -> str:
        self._post_seq += 1
        return f"bot-{self._post_seq}"

    async def read_thread(self, thread_root_id: str) -> Sequence[ChatMessage]:
        return []

    async def send_direct(self, user_id: str, text: str) -> ChatMessage:
        post_id = self._next()
        self.sent_dms.append((user_id, text))
        return ChatMessage(
            id=post_id, channel_id=f"dm-{user_id}", author_id="uid-bot",
            text=text, timestamp=datetime.now(timezone.utc), trusted=True,
        )

    async def send_to_channel(
        self, channel_id: str, text: str, thread_root_id: str | None = None,
    ) -> ChatMessage:
        return ChatMessage(
            id=self._next(), channel_id=channel_id, author_id="uid-bot",
            text=text, timestamp=datetime.now(timezone.utc), trusted=True,
            thread_root_id=thread_root_id,
        )

    async def find_user_by_email(self, email: str) -> ChatUser | None:
        for username, uid in self._users.items():
            if username == email.split("@", 1)[0]:
                return ChatUser(id=uid, username=username, email=email)
        return None

    async def find_user_by_username(self, username: str) -> ChatUser | None:
        uid = self._users.get(username)
        return ChatUser(id=uid, username=username) if uid else None

    async def add_reaction(self, post_id: str, emoji_name: str) -> None:
        self.reactions.append((post_id, emoji_name))

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


class _ScriptedPlanner:
    def __init__(self, decisions: list[PlannerDecision]) -> None:
        self._decisions = list(decisions)
        self.calls = 0

    async def decide(self, inp: Any) -> PlannerDecision:
        self.calls += 1
        if not self._decisions:
            raise AssertionError(
                f"planner called {self.calls} times but no scripted decision left"
            )
        return self._decisions.pop(0)


class _RecordingBus(MessageBusPort):
    def __init__(self) -> None:
        self.published: list[AgentMessage] = []

    async def publish(self, message: AgentMessage) -> None:
        self.published.append(message)

    def subscribe(self, agent_key: str) -> AsyncIterator[AgentMessage]:  # pragma: no cover
        async def _gen() -> AsyncIterator[AgentMessage]:
            if False:
                yield  # type: ignore[unreachable]
        return _gen()


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
                poll_interval_seconds=10,
                max_planner_calls_per_goal=8,
                max_goal_age_hours=48,
                send_retry_max=3,
                replanning_stuck_after_minutes=10,
            ),
        ),
        mappings=MappingsCfg(),
        notifications=NotificationsCfg(mattermost=MmTemplatesCfg()),
    )


async def _seed_task(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[TaskRow, PlanRow]:
    async with session_scope(session_factory) as session:
        task = TaskRow(
            tracker="jira", external_id="DM-42",
            title="Bug DM-42", description="something is broken",
            url="https://jira/DM-42",
        )
        session.add(task)
        await session.flush()
        plan = PlanRow(
            tracker="jira", task_external_id="DM-42",
            summary="x", target_repo_key=None,
        )
        session.add(plan)
        await session.flush()
        return task, plan


def _make_orchestrator(
    session_factory: async_sessionmaker[AsyncSession],
    chat: ChatPort,
    planner: _ScriptedPlanner,
    bus: MessageBusPort,
) -> GoalOrchestrator:
    return GoalOrchestrator(
        repo=GoalRepository(session_factory),
        communicator=CommunicatorService(
            chat, InjectionFilter(), respect_working_hours=False,
        ),
        planner=planner,                                 # type: ignore[arg-type]
        config=_config(),
        session_factory=session_factory,
        message_bus=bus,
    )


def _make_listener(
    session_factory: async_sessionmaker[AsyncSession],
    chat: ChatPort,
    orchestrator: GoalOrchestrator,
) -> MmThreadListener:
    return MmThreadListener(
        chat=chat,
        communicator=CommunicatorService(
            chat, InjectionFilter(), respect_working_hours=False,
        ),
        responder=None,                                  # type: ignore[arg-type]
        dev_agents={},
        session_factory=session_factory,
        config=_config(),
        settings=Settings(),
        goal_orchestrator=orchestrator,
    )


# ============================================================
#                          The full cycle
# ============================================================


@pytest.mark.asyncio
async def test_full_cycle_from_open_question_to_re_dispatch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task, plan_row = await _seed_task(session_factory)

    chat = _Chat(users={
        "team-lead": "uid-lead",
        "v.kura": "uid-vasya",
    })
    bus = _RecordingBus()

    # 4 planner calls scripted (one per decision point):
    #   1. Initial → ASK team-lead.
    #   2. After lead's reply (@v.kura) → ASK v.kura, fresh question.
    #   3. After v.kura's reply (body example) → ACHIEVE.
    planner = _ScriptedPlanner([
        PlannerDecision(
            action=PlannerActionKind.ASK,
            reasoning="hint says team-lead knows",
            to_handle="team-lead",
            message="Кто в курсе про body для DM-42?",
            dedupe_key="lead:body-owner",
        ),
        PlannerDecision(
            action=PlannerActionKind.ASK,
            reasoning="lead pointed at v.kura — ask Vasya the actual body question",
            to_handle="v.kura",
            message="Привет, Вася! Подскажи пример request body для DM-42, нужно для воспроизведения.",
            dedupe_key="vkura:body-example",
        ),
        PlannerDecision(
            action=PlannerActionKind.ACHIEVE,
            reasoning="Vasya gave the body",
            final_answer='POST /api/v1/tasks {"x":1}',
            confidence=0.9,
        ),
    ])
    orch = _make_orchestrator(session_factory, chat, planner, bus)
    listener = _make_listener(session_factory, chat, orch)
    repo = GoalRepository(session_factory)

    # ---------- 1. Analyst hands an open question
    plan = Plan(
        task_external_id="DM-42", tracker="jira",
        summary="...",
        open_questions=[OpenQuestion(
            question="нужен пример body для DM-42",
            why_it_matters="without body we can't repro",
            ask_whom="team-lead",
        )],
        status=PlanStatus.CLARIFYING,
    )
    created = await orch.request_clarifications(
        task_row=task, plan=plan, plan_row_id=plan_row.id,
    )
    assert created == 1
    assert planner.calls == 1
    # First DM landed on team-lead.
    assert chat.sent_dms == [(
        "uid-lead",
        "Кто в курсе про body для DM-42?",
    )]
    goals = await repo.list_for_plan(plan_row.id)
    assert len(goals) == 1
    g = goals[0]
    assert g.state == GoalState.AWAITING_REPLY
    assert g.current_target_user_id == "uid-lead"
    lead_post_id = g.current_asked_post_id
    assert lead_post_id is not None

    # ---------- 2. team-lead replies through MmThreadListener
    lead_reply = ChatMessage(
        id="reply-lead", channel_id="dm-uid-lead", author_id="uid-lead",
        text="спроси Васю Курочкина, @v.kura",
        timestamp=datetime.now(timezone.utc),
        thread_root_id=lead_post_id, trusted=False,
    )
    await listener._dispatch(lead_reply)  # noqa: SLF001
    g = (await repo.get(g.id))
    assert g is not None
    assert g.state == GoalState.COALESCING

    # ---------- 3. coalescer ticks → planner runs again with the reply
    # Backdate last_fragment_at so flush_idle picks the goal up.
    async with session_scope(session_factory) as session:
        from virtual_dev.infrastructure.db import GoalRow
        row = (await session.execute(
            select(GoalRow).where(GoalRow.id == g.id)
        )).scalar_one()
        row.last_fragment_at = datetime.now(timezone.utc) - timedelta(seconds=5)

    flushed = await orch.flush_idle()
    assert flushed == 1
    assert planner.calls == 2
    # Second DM lands on Vasya — and is **freshly composed**.
    assert len(chat.sent_dms) == 2
    target_uid, vasya_text = chat.sent_dms[1]
    assert target_uid == "uid-vasya"
    assert "body" in vasya_text.lower() or "пример" in vasya_text.lower()
    # Critically: the new ask is NOT a copy of the lead-question text.
    assert "Кто в курсе" not in vasya_text
    g = await repo.get(g.id)
    assert g is not None
    assert g.state == GoalState.AWAITING_REPLY
    assert g.current_target_user_id == "uid-vasya"
    vasya_post_id = g.current_asked_post_id
    assert vasya_post_id is not None
    # ✅-reaction posted on lead's reply.
    assert ("reply-lead", "white_check_mark") in chat.reactions

    # ---------- 4. Vasya replies with the body — through listener
    vasya_reply = ChatMessage(
        id="reply-vasya", channel_id="dm-uid-vasya", author_id="uid-vasya",
        text='POST /api/v1/tasks {"x":1}',
        timestamp=datetime.now(timezone.utc),
        thread_root_id=vasya_post_id, trusted=False,
    )
    await listener._dispatch(vasya_reply)  # noqa: SLF001

    # Backdate again, flush.
    async with session_scope(session_factory) as session:
        from virtual_dev.infrastructure.db import GoalRow
        row = (await session.execute(
            select(GoalRow).where(GoalRow.id == g.id)
        )).scalar_one()
        row.last_fragment_at = datetime.now(timezone.utc) - timedelta(seconds=5)

    flushed = await orch.flush_idle()
    assert flushed == 1
    assert planner.calls == 3
    g = await repo.get(g.id)
    assert g is not None
    assert g.state == GoalState.ACHIEVED
    assert g.final_answer == 'POST /api/v1/tasks {"x":1}'

    # ---------- 5. plan settled → task.discovered re-published
    publishes = [m for m in bus.published if m.topic == "task.discovered"]
    assert len(publishes) == 1
    assert publishes[0].payload == {
        "tracker": "jira", "external_id": "DM-42",
    }

    # Task description got the answer folded in.
    async with session_scope(session_factory) as session:
        refreshed = (await session.execute(
            select(TaskRow).where(TaskRow.external_id == "DM-42")
        )).scalar_one()
        assert "Уточнения" in refreshed.description
        assert 'POST /api/v1/tasks' in refreshed.description

        # Plan is SUPERSEDED so Analyst can replan freely.
        refreshed_plan = (await session.execute(
            select(PlanRow).where(PlanRow.id == plan_row.id)
        )).scalar_one()
        assert refreshed_plan.status == PlanStatus.SUPERSEDED.value


@pytest.mark.asyncio
async def test_dispatch_routes_dm_with_no_thread_root_to_most_recent_goal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When a person replies in a DM channel WITHOUT quoting the bot's
    post (no thread_root_id), the listener still routes the reply to
    the most-recent active goal for that channel + user."""
    task, plan_row = await _seed_task(session_factory)
    chat = _Chat(users={"alice": "uid-alice"})
    bus = _RecordingBus()
    planner = _ScriptedPlanner([
        PlannerDecision(
            action=PlannerActionKind.ASK, reasoning="x",
            to_handle="alice",
            message="Подскажи про body, плиз",
            dedupe_key="alice:body",
        ),
    ])
    orch = _make_orchestrator(session_factory, chat, planner, bus)
    listener = _make_listener(session_factory, chat, orch)

    plan = Plan(
        task_external_id="DM-42", tracker="jira", summary="x",
        open_questions=[OpenQuestion(question="что-то", ask_whom="alice")],
        status=PlanStatus.CLARIFYING,
    )
    await orch.request_clarifications(
        task_row=task, plan=plan, plan_row_id=plan_row.id,
    )
    repo = GoalRepository(session_factory)
    goal = (await repo.list_for_plan(plan_row.id))[0]

    # Alice replies in the channel but doesn't quote the bot.
    reply_no_thread = ChatMessage(
        id="reply-no-thread", channel_id="dm-uid-alice", author_id="uid-alice",
        text="вот ответ",
        timestamp=datetime.now(timezone.utc),
        thread_root_id=None, trusted=False,
    )
    await listener._dispatch(reply_no_thread)  # noqa: SLF001

    # Goal is now COALESCING with the fragment buffered.
    refreshed = await repo.get(goal.id)
    assert refreshed is not None
    assert refreshed.state == GoalState.COALESCING
    pending = await repo.list_unflushed_fragments(goal.id)
    assert [p.text for p in pending] == ["вот ответ"]


@pytest.mark.asyncio
async def test_dispatch_ignores_bot_authored_posts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Trusted (bot-authored) events must never become fragments."""
    task, plan_row = await _seed_task(session_factory)
    chat = _Chat(users={"alice": "uid-alice"})
    bus = _RecordingBus()
    planner = _ScriptedPlanner([
        PlannerDecision(
            action=PlannerActionKind.ASK, reasoning="x",
            to_handle="alice", message="Q", dedupe_key="alice:q",
        ),
    ])
    orch = _make_orchestrator(session_factory, chat, planner, bus)
    listener = _make_listener(session_factory, chat, orch)

    plan = Plan(
        task_external_id="DM-42", tracker="jira", summary="x",
        open_questions=[OpenQuestion(question="x", ask_whom="alice")],
        status=PlanStatus.CLARIFYING,
    )
    await orch.request_clarifications(
        task_row=task, plan=plan, plan_row_id=plan_row.id,
    )

    own = ChatMessage(
        id="own", channel_id="dm-uid-alice", author_id="uid-bot",
        text="my own message", timestamp=datetime.now(timezone.utc),
        thread_root_id=None, trusted=True,
    )
    await listener._dispatch(own)  # noqa: SLF001

    repo = GoalRepository(session_factory)
    goal = (await repo.list_for_plan(plan_row.id))[0]
    pending = await repo.list_unflushed_fragments(goal.id)
    assert pending == []
