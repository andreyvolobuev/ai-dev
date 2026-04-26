"""Tests for the Phase 4.6 ClarificationAgent + TaskOrchestrator.

These tests pin the contract of the single-agent loop:

* Agent run produces effects (ask_dispatched / final_answer / escalate
  / abandon); orchestrator translates effects into DB writes.
* When the agent calls ``submit_final_answer``, the task is marked
  solved.
* When the agent calls ``ask_mm_user``, the task gets ``awaiting_*``
  installed and waits for a coalesced reply.
* On the next coalesced reply, the orchestrator re-invokes the agent;
  the agent's prompt now includes the prior history so it can react.
* When the agent finishes a run with NO terminal effect (max_turns
  hit or model bailed), the orchestrator escalates so silent dead-air
  is visible.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents.clarification_agent import (
    AgentEffect,
    AgentRunInput,
    AgentRunResult,
    ClarificationAgent,
)
from virtual_dev.application.services.clarification import (
    ClarificationTaskRepository,
    TaskOrchestrator,
)
from virtual_dev.application.services.communicator import CommunicatorService
from virtual_dev.application.services.injection_filter import InjectionFilter
from virtual_dev.domain.models.chat import ChatMessage, ChatUser
from virtual_dev.domain.models.plan import OpenQuestion, Plan, PlanStatus
from virtual_dev.domain.ports.chat import ChatPort
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


# ============================================================
#                          Fakes
# ============================================================


class _FakeChat(ChatPort):
    def __init__(self, *, users: dict[str, str] | None = None) -> None:
        self._users = users or {}
        self.sent_dms: list[tuple[str, str]] = []
        self._post_seq = 0

    def _next(self) -> str:
        self._post_seq += 1
        return f"bot-post-{self._post_seq}"

    async def read_thread(self, thread_root_id: str) -> Sequence[ChatMessage]:
        return []

    async def send_direct(self, user_id: str, text: str) -> ChatMessage:
        self.sent_dms.append((user_id, text))
        return ChatMessage(
            id=self._next(), channel_id=f"dm-{user_id}",
            author_id="uid-bot", text=text,
            timestamp=datetime.now(timezone.utc), trusted=True,
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


class _ScriptedAgent:
    """Drop-in for ClarificationAgent: returns scripted effect lists per run."""

    def __init__(self, scripted_runs: list[list[AgentEffect]]) -> None:
        self._runs = list(scripted_runs)
        self.calls: list[AgentRunInput] = []

    async def run(self, inp: AgentRunInput) -> AgentRunResult:
        self.calls.append(inp)
        if not self._runs:
            raise AssertionError("agent called more times than scripted")
        effects = self._runs.pop(0)
        return AgentRunResult(
            effects=list(effects),
            cost_usd=0.01,
            turns=len(effects) + 1,
            stopped_reason="end_turn",
        )


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
                max_subgoal_depth=4,
            ),
        ),
        mappings=MappingsCfg(),
        notifications=NotificationsCfg(mattermost=MmTemplatesCfg()),
    )


async def _seed_task_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[TaskRow, PlanRow]:
    async with session_scope(session_factory) as session:
        task = TaskRow(
            tracker="jira", external_id="DM-42",
            title="Bug DM-42", description="нужен пример body",
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
    agent: _ScriptedAgent,
) -> TaskOrchestrator:
    return TaskOrchestrator(
        repo=ClarificationTaskRepository(session_factory),
        communicator=CommunicatorService(
            chat, InjectionFilter(), respect_working_hours=False,
        ),
        agent=agent,                                     # type: ignore[arg-type]
        config=_config(),
        session_factory=session_factory,
        message_bus=None,
    )


# ============================================================
#                          Tests
# ============================================================


@pytest.mark.asyncio
async def test_agent_final_answer_marks_task_solved(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One agent run that calls ``submit_final_answer`` closes the task
    with ``is_solved=True`` and the answer/confidence stored."""
    task_row, plan_row = await _seed_task_row(session_factory)
    chat = _FakeChat()

    agent = _ScriptedAgent([
        [AgentEffect(
            kind="final_answer",
            payload={
                "final_answer": "POST /api/v1/tasks {x:1}",
                "confidence": 0.9,
                "reasoning": "found in repo",
            },
        )],
    ])
    orch = _make_orchestrator(session_factory, chat, agent)

    plan = Plan(
        task_external_id="DM-42", tracker="jira", summary="x",
        open_questions=[OpenQuestion(question="нужен body", ask_whom=None)],
        status=PlanStatus.CLARIFYING,
    )
    await orch.request_clarifications(
        task_row=task_row, plan=plan, plan_row_id=plan_row.id,
    )

    repo = ClarificationTaskRepository(session_factory)
    [t] = await repo.list_for_plan(plan_row.id)
    assert t.is_solved is True
    assert t.closed is True
    assert t.final_answer == "POST /api/v1/tasks {x:1}"
    assert t.confidence == pytest.approx(0.9)
    assert chat.sent_dms == []   # no DM sent — agent answered from research


@pytest.mark.asyncio
async def test_agent_ask_dispatches_dm_and_installs_awaiting(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """After the agent's run produced an ``ask_dispatched`` effect, the
    task has ``awaiting_*`` installed and stays open."""
    task_row, plan_row = await _seed_task_row(session_factory)
    chat = _FakeChat()  # We bypass real ASK by feeding the effect directly.

    agent = _ScriptedAgent([
        [AgentEffect(
            kind="ask_dispatched",
            payload={
                "asked_post_id": "post-1",
                "channel_id": "dm-uid-alice",
                "target_user_id": "uid-alice",
                "target_username": "alice",
                "asked_text": "что там с body?",
                "dedupe_key": "alice:body",
            },
        )],
    ])
    orch = _make_orchestrator(session_factory, chat, agent)

    plan = Plan(
        task_external_id="DM-42", tracker="jira", summary="x",
        open_questions=[OpenQuestion(question="body", ask_whom=None)],
        status=PlanStatus.CLARIFYING,
    )
    await orch.request_clarifications(
        task_row=task_row, plan=plan, plan_row_id=plan_row.id,
    )

    repo = ClarificationTaskRepository(session_factory)
    [t] = await repo.list_for_plan(plan_row.id)
    assert t.closed is False
    assert t.is_solved is False
    assert t.awaiting_post_id == "post-1"
    assert t.awaiting_user_id == "uid-alice"
    assert t.info_source == "alice"
    assert t.info_source_class == "mattermost"


@pytest.mark.asyncio
async def test_agent_re_invoked_after_coalesced_reply(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When fragments coalesce, the orchestrator re-runs the agent with
    the merged reply available in history."""
    task_row, plan_row = await _seed_task_row(session_factory)
    chat = _FakeChat()

    agent = _ScriptedAgent([
        # Run 1: agent ASKs.
        [AgentEffect(
            kind="ask_dispatched",
            payload={
                "asked_post_id": "post-1",
                "channel_id": "dm-uid-alice",
                "target_user_id": "uid-alice",
                "target_username": "alice",
                "asked_text": "что там с body?",
                "dedupe_key": "alice:body",
            },
        )],
        # Run 2 (after reply coalesces): agent submits final answer.
        [AgentEffect(
            kind="final_answer",
            payload={
                "final_answer": "POST /tasks {x:1}",
                "confidence": 0.9,
                "reasoning": "alice answered",
            },
        )],
    ])
    orch = _make_orchestrator(session_factory, chat, agent)

    plan = Plan(
        task_external_id="DM-42", tracker="jira", summary="x",
        open_questions=[OpenQuestion(question="body", ask_whom=None)],
        status=PlanStatus.CLARIFYING,
    )
    await orch.request_clarifications(
        task_row=task_row, plan=plan, plan_row_id=plan_row.id,
    )

    repo = ClarificationTaskRepository(session_factory)
    [t] = await repo.list_for_plan(plan_row.id)
    # Now alice "replies".
    reply = ChatMessage(
        id="alice-reply", channel_id="dm-uid-alice", author_id="uid-alice",
        text='POST /tasks {"x":1}',
        timestamp=datetime.now(timezone.utc),
        thread_root_id="post-1", trusted=False,
    )
    await orch.append_fragment(t.id, reply)
    # Force coalesce window past.
    from sqlalchemy import select

    from virtual_dev.infrastructure.db import TaskRowClar
    async with session_scope(session_factory) as session:
        row = (await session.execute(
            select(TaskRowClar).where(TaskRowClar.id == t.id)
        )).scalar_one()
        row.last_fragment_at = datetime.now(timezone.utc) - timedelta(seconds=5)

    await orch.flush_idle()

    # Agent was re-invoked. Run #2's prompt includes the HUMAN_REPLIED
    # step — the agent saw alice's text and chose to submit_final_answer.
    assert len(agent.calls) == 2
    second_history_kinds = {s.kind.value for s in agent.calls[1].history}
    assert "human_replied" in second_history_kinds

    refreshed = await repo.get(t.id)
    assert refreshed is not None
    assert refreshed.is_solved is True
    assert refreshed.final_answer == "POST /tasks {x:1}"


@pytest.mark.asyncio
async def test_agent_escalates_when_run_produces_no_effect(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Silent agent (max_turns hit, no terminal effect) must not leave
    the task hanging — the orchestrator escalates."""
    task_row, plan_row = await _seed_task_row(session_factory)
    chat = _FakeChat(users={"lead": "uid-lead"})

    agent = _ScriptedAgent([
        [],   # no effects whatsoever
    ])
    orch = _make_orchestrator(session_factory, chat, agent)

    plan = Plan(
        task_external_id="DM-42", tracker="jira", summary="x",
        open_questions=[OpenQuestion(question="body", ask_whom=None)],
        status=PlanStatus.CLARIFYING,
    )
    await orch.request_clarifications(
        task_row=task_row, plan=plan, plan_row_id=plan_row.id,
    )

    repo = ClarificationTaskRepository(session_factory)
    [t] = await repo.list_for_plan(plan_row.id)
    assert t.closed is True
    assert t.is_solved is False
    # Lead got the escalation DM.
    lead_dms = [(u, _) for (u, _) in chat.sent_dms if u == "uid-lead"]
    assert len(lead_dms) == 1


@pytest.mark.asyncio
async def test_agent_abandon_closes_without_lead_dm(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_row, plan_row = await _seed_task_row(session_factory)
    chat = _FakeChat(users={"lead": "uid-lead"})

    agent = _ScriptedAgent([
        [AgentEffect(kind="abandon", payload={"reason": "obsolete"})],
    ])
    orch = _make_orchestrator(session_factory, chat, agent)

    plan = Plan(
        task_external_id="DM-42", tracker="jira", summary="x",
        open_questions=[OpenQuestion(question="x", ask_whom=None)],
        status=PlanStatus.CLARIFYING,
    )
    await orch.request_clarifications(
        task_row=task_row, plan=plan, plan_row_id=plan_row.id,
    )

    repo = ClarificationTaskRepository(session_factory)
    [t] = await repo.list_for_plan(plan_row.id)
    assert t.closed is True
    assert t.is_solved is False
    # Abandon ≠ escalate: NO DM to lead.
    lead_dms = [(u, _) for (u, _) in chat.sent_dms if u == "uid-lead"]
    assert lead_dms == []


@pytest.mark.asyncio
async def test_max_iterations_hard_cap_escalates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Even if the agent keeps refusing to terminate (e.g. always asks
    again), the orchestrator's iteration_count cap stops it."""
    task_row, plan_row = await _seed_task_row(session_factory)
    chat = _FakeChat(users={"lead": "uid-lead"})

    repo = ClarificationTaskRepository(session_factory)
    # Manually create a task at the cap.
    cfg = _config()
    cfg.agents.clarification.max_planner_calls_per_goal = 2
    deadline = datetime.now(timezone.utc) + timedelta(hours=48)
    seeded = await repo.create_task(
        plan_id=plan_row.id, parent_id=None,
        tracker="jira", task_external_id="DM-42",
        question="body", info_source=None, info_source_class=None,
        coalesce_window_seconds=60, deadline_at=deadline,
    )
    await repo.update(seeded.id, iteration_count_delta=2)

    agent = _ScriptedAgent([])  # never called
    orch = TaskOrchestrator(
        repo=repo,
        communicator=CommunicatorService(
            chat, InjectionFilter(), respect_working_hours=False,
        ),
        agent=agent,                                     # type: ignore[arg-type]
        config=cfg,
        session_factory=session_factory,
        message_bus=None,
    )
    refreshed = await repo.get(seeded.id)
    assert refreshed is not None
    await orch._drive(refreshed)  # noqa: SLF001

    final = await repo.get(seeded.id)
    assert final is not None
    assert final.closed is True
    assert final.is_solved is False
    # Lead DM'd because escalate=True for max-iterations.
    lead_dms = [(u, _) for (u, _) in chat.sent_dms if u == "uid-lead"]
    assert len(lead_dms) == 1
