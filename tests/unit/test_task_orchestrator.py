"""Tests for the task-driven clarification orchestrator (Phase 4.5).

Covers the core loop:

  pick → apply tool → validate → mark solved or retry

Plus chain validation: a response that resolves an ANCESTOR task
(skipping levels) closes the ancestor and skips the rest of the
chain. This is the "shortcut" the user explicitly called out:
asking the reporter for Vasya's MM handle, the reporter pastes the
body example directly — root task closes, no need to chase Vasya.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents.clarification_validator import (
    ValidatorOutput,
    ValidatorVerdict,
)
from virtual_dev.application.services.clarification import (
    ClarificationTaskRepository,
    TaskOrchestrator,
)
from virtual_dev.application.services.clarification.tools import (
    discover_builtin_tools,
    get_tool_registry,
)
from virtual_dev.application.services.communicator import CommunicatorService
from virtual_dev.application.services.injection_filter import InjectionFilter
from virtual_dev.domain.models.chat import ChatMessage, ChatUser
from virtual_dev.domain.models.clarification_task import (
    ClarificationTask,
    ToolInvocation,
)
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

    async def search_users_by_name(
        self, query: str, *, limit: int = 25,
    ) -> Sequence[ChatUser]:
        # Substring match on username.
        return [
            ChatUser(id=uid, username=u)
            for u, uid in self._users.items()
            if query.lower() in u.lower()
        ]


class _ScriptedPicker:
    def __init__(self, picks: list[ToolInvocation]) -> None:
        self._picks = list(picks)
        self.calls = 0

    async def pick(self, inp: Any) -> ToolInvocation:
        self.calls += 1
        if not self._picks:
            raise AssertionError("picker called more times than scripted")
        return self._picks.pop(0)


class _ScriptedValidator:
    def __init__(self, outputs: list[ValidatorOutput]) -> None:
        self._outputs = list(outputs)
        self.calls = 0

    async def validate(self, inp: Any) -> ValidatorOutput:
        self.calls += 1
        if not self._outputs:
            raise AssertionError("validator called more times than scripted")
        return self._outputs.pop(0)


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


async def _seed_task(
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
    picker: _ScriptedPicker,
    validator: _ScriptedValidator,
) -> TaskOrchestrator:
    # Make sure the tool registry is populated (test isolation may
    # have cleared it).
    discover_builtin_tools()
    return TaskOrchestrator(
        repo=ClarificationTaskRepository(session_factory),
        communicator=CommunicatorService(
            chat, InjectionFilter(), respect_working_hours=False,
        ),
        picker=picker,                                  # type: ignore[arg-type]
        validator=validator,                            # type: ignore[arg-type]
        config=_config(),
        session_factory=session_factory,
        message_bus=None,
        tool_registry=get_tool_registry(),
    )


# ============================================================
#                     Tool-pick happy path
# ============================================================


@pytest.mark.asyncio
async def test_sync_tool_solves_task_via_validator(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Picker picks find_mm_user_by_name; the SYNC tool returns a
    match; validator says task #1 resolved; task closes."""
    task_row, plan_row = await _seed_task(session_factory)
    chat = _FakeChat(users={"v.kura": "uid-vkura"})

    picker = _ScriptedPicker([
        ToolInvocation(
            tool="find_mm_user_by_name",
            params={"query": "kura", "limit": 5},
            reasoning="search for the handle",
        ),
    ])
    validator = _ScriptedValidator([
        ValidatorOutput(
            resolves=[ValidatorVerdict(
                task_id=1, final_answer="@v.kura", confidence=0.9,
            )],
            reasoning="single match found",
        ),
    ])
    orch = _make_orchestrator(session_factory, chat, picker, validator)

    plan = Plan(
        task_external_id="DM-42", tracker="jira", summary="x",
        open_questions=[OpenQuestion(question="MM-handle Васи", ask_whom=None)],
        status=PlanStatus.CLARIFYING,
    )
    await orch.request_clarifications(
        task_row=task_row, plan=plan, plan_row_id=plan_row.id,
    )

    repo = ClarificationTaskRepository(session_factory)
    tasks = await repo.list_for_plan(plan_row.id)
    assert len(tasks) == 1
    t = tasks[0]
    assert t.is_solved is True
    assert t.closed is True
    assert t.final_answer == "@v.kura"
    assert chat.sent_dms == []  # no DM, validator closed via tool result


@pytest.mark.asyncio
async def test_sync_tool_ambiguous_result_loops_to_next_pick(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Validator returns empty resolves → tool added to tools_tried,
    picker is called again."""
    task_row, plan_row = await _seed_task(session_factory)
    chat = _FakeChat(users={"v.kura": "uid-vkura", "v.kuznetsov": "uid-vkuznetsov"})

    picker = _ScriptedPicker([
        ToolInvocation(
            tool="find_mm_user_by_name",
            params={"query": "v", "limit": 10},
            reasoning="initial search",
        ),
        ToolInvocation(
            tool="abandon", params={"reason": "ambiguous"}, reasoning="give up",
        ),
    ])
    validator = _ScriptedValidator([
        ValidatorOutput(resolves=[], reasoning="ambiguous; 2 candidates"),
    ])
    orch = _make_orchestrator(session_factory, chat, picker, validator)

    plan = Plan(
        task_external_id="DM-42", tracker="jira", summary="x",
        open_questions=[OpenQuestion(question="MM-handle", ask_whom=None)],
        status=PlanStatus.CLARIFYING,
    )
    await orch.request_clarifications(
        task_row=task_row, plan=plan, plan_row_id=plan_row.id,
    )

    repo = ClarificationTaskRepository(session_factory)
    tasks = await repo.list_for_plan(plan_row.id)
    assert len(tasks) == 1
    t = tasks[0]
    assert t.is_solved is False
    assert t.closed is True  # abandoned
    assert "find_mm_user_by_name" in t.tools_tried
    assert picker.calls == 2  # second pick after the ambiguous result


# ============================================================
#                  ASYNC tool flow
# ============================================================


@pytest.mark.asyncio
async def test_async_tool_dispatches_dm_and_installs_awaiting(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ask_mm_user sends a DM and parks the task waiting on a reply."""
    task_row, plan_row = await _seed_task(session_factory)
    chat = _FakeChat(users={"alice": "uid-alice"})

    picker = _ScriptedPicker([
        ToolInvocation(
            tool="ask_mm_user",
            params={
                "to_handle": "alice",
                "message": "Подскажи, что там с body?",
                "dedupe_key": "alice:body",
            },
            reasoning="ask alice directly",
        ),
    ])
    validator = _ScriptedValidator([])
    orch = _make_orchestrator(session_factory, chat, picker, validator)

    plan = Plan(
        task_external_id="DM-42", tracker="jira", summary="x",
        open_questions=[OpenQuestion(question="нужен пример body", ask_whom=None)],
        status=PlanStatus.CLARIFYING,
    )
    await orch.request_clarifications(
        task_row=task_row, plan=plan, plan_row_id=plan_row.id,
    )

    repo = ClarificationTaskRepository(session_factory)
    [t] = await repo.list_for_plan(plan_row.id)
    assert chat.sent_dms == [("uid-alice", "Подскажи, что там с body?")]
    assert t.awaiting_user_id == "uid-alice"
    assert t.awaiting_username == "alice"
    assert t.info_source == "alice"
    assert t.info_source_class == "mattermost"
    assert t.closed is False


# ============================================================
#                  META: decompose
# ============================================================


@pytest.mark.asyncio
async def test_decompose_creates_subtasks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_row, plan_row = await _seed_task(session_factory)
    chat = _FakeChat()

    picker = _ScriptedPicker([
        # parent decides to decompose
        ToolInvocation(
            tool="decompose",
            params={"subtasks": [
                {"question": "найти @-handle Васи"},
                {"question": "найти test environment"},
            ]},
            reasoning="composite goal",
        ),
        # child A picks abandon (we don't care for this test)
        ToolInvocation(tool="abandon", params={"reason": "x"}, reasoning="x"),
        # child B picks abandon
        ToolInvocation(tool="abandon", params={"reason": "y"}, reasoning="y"),
        # parent re-runs after both children terminal — picks abandon
        ToolInvocation(tool="abandon", params={"reason": "z"}, reasoning="z"),
    ])
    validator = _ScriptedValidator([])
    orch = _make_orchestrator(session_factory, chat, picker, validator)

    plan = Plan(
        task_external_id="DM-42", tracker="jira", summary="x",
        open_questions=[OpenQuestion(question="нужен пример body", ask_whom=None)],
        status=PlanStatus.CLARIFYING,
    )
    await orch.request_clarifications(
        task_row=task_row, plan=plan, plan_row_id=plan_row.id,
    )

    repo = ClarificationTaskRepository(session_factory)
    all_tasks = await repo.list_for_plan(plan_row.id)
    assert len(all_tasks) == 3   # parent + 2 children
    parent = next(t for t in all_tasks if t.parent_id is None)
    children = [t for t in all_tasks if t.parent_id == parent.id]
    assert len(children) == 2
    assert all(c.depth == 1 for c in children)


# ============================================================
#                  Chain validation (the user's spec)
# ============================================================


@pytest.mark.asyncio
async def test_chain_validation_resolves_ancestor_via_descendant_response(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The user's example: parent task = "get body example", child =
    "find Vasya's handle". When the reporter (a respondent on the
    child) pastes the body directly, the validator marks the PARENT
    solved, and the parent doesn't re-run — the chain is closed."""
    task_row, plan_row = await _seed_task(session_factory)
    chat = _FakeChat(users={"alice": "uid-alice"})

    picker = _ScriptedPicker([
        # Parent: decompose into "find Vasya's handle".
        ToolInvocation(
            tool="decompose",
            params={"subtasks": [
                {"question": "найти @-handle Васи"},
            ]},
            reasoning="prereq",
        ),
        # Child: ask_mm_user(alice).
        ToolInvocation(
            tool="ask_mm_user",
            params={
                "to_handle": "alice",
                "message": "Кто такой Вася Курочкин?",
                "dedupe_key": "alice:vasya-handle",
            },
            reasoning="ask reporter",
        ),
    ])
    validator = _ScriptedValidator([
        # Validator sees alice's reply on the child task; recognises
        # it answers the PARENT (root, id=1) directly.
        ValidatorOutput(
            resolves=[
                ValidatorVerdict(
                    task_id=2,
                    final_answer="Это Вася Кузнецов (@v.kuznetsov)",
                    confidence=0.85,
                ),
                ValidatorVerdict(
                    task_id=1,
                    final_answer="POST /tasks {...}",
                    confidence=0.9,
                ),
            ],
            reasoning="reporter answered the body directly; chain shortcut",
        ),
    ])
    orch = _make_orchestrator(session_factory, chat, picker, validator)

    plan = Plan(
        task_external_id="DM-42", tracker="jira", summary="x",
        open_questions=[OpenQuestion(question="нужен пример body", ask_whom=None)],
        status=PlanStatus.CLARIFYING,
    )
    await orch.request_clarifications(
        task_row=task_row, plan=plan, plan_row_id=plan_row.id,
    )

    repo = ClarificationTaskRepository(session_factory)
    parent = (await repo.list_for_plan(plan_row.id))[0]
    [child] = await repo.list_subtasks(parent.id)

    # Child is awaiting the DM-reply at this point.
    assert child.closed is False
    assert child.awaiting_user_id == "uid-alice"

    # Simulate alice's reply: orchestrator's coalesce path will fold
    # fragments and call the validator on the merged text. We bypass
    # the coalesce timer by appending the fragment + flushing manually.
    reply = ChatMessage(
        id="alice-reply", channel_id=child.awaiting_channel_id,
        author_id="uid-alice",
        text="Это Вася Кузнецов (@v.kuznetsov), и body такой: POST /tasks {...}",
        timestamp=datetime.now(timezone.utc),
        thread_root_id=child.awaiting_post_id, trusted=False,
    )
    await orch.append_fragment(child.id, reply)
    # Force the coalesce window past.
    from sqlalchemy import select

    from virtual_dev.infrastructure.db import TaskRowClar
    async with session_scope(session_factory) as session:
        row = (await session.execute(
            select(TaskRowClar).where(TaskRowClar.id == child.id)
        )).scalar_one()
        row.last_fragment_at = datetime.now(timezone.utc) - timedelta(seconds=5)

    await orch.flush_idle()

    # Both tasks resolved.
    parent_after = await repo.get(parent.id)
    child_after = await repo.get(child.id)
    assert parent_after is not None and child_after is not None
    assert parent_after.is_solved is True
    assert "POST /tasks" in (parent_after.final_answer or "")
    assert child_after.is_solved is True
    assert "Кузнецов" in (child_after.final_answer or "")
