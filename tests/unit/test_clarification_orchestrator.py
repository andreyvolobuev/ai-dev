"""ClarificationOrchestrator — state machine + loop guards + re-publish.

The classifier and counter-answerer are stubbed: each test feeds canned
``ClassificationResult`` / ``CounterAnswerResult`` objects so the
state machine is what we exercise.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents.answer_classifier import ClassificationResult
from virtual_dev.application.agents.counter_answerer import CounterAnswerResult
from virtual_dev.application.services import CommunicatorService, InjectionFilter
from virtual_dev.application.services.clarification import ClarificationOrchestrator
from virtual_dev.application.services.clarification.repo import QuestionRepository
from virtual_dev.application.services.clarification.stakeholder_resolver import (
    StakeholderResolver,
)
from virtual_dev.domain.models.chat import ChatMessage, ChatUser
from virtual_dev.domain.models.clarification import (
    Classification,
    CounterQuestionKind,
    OutOfScopeKind,
    Question,
    QuestionState,
    Stakeholder,
    StakeholderKind,
)
from virtual_dev.domain.models.plan import OpenQuestion, Plan, PlanStatus
from virtual_dev.domain.ports.chat import ChatPort
from virtual_dev.domain.ports.message_bus import AgentMessage, MessageBusPort
from virtual_dev.infrastructure.config import (
    AgentsCfg,
    AppConfig,
    MappingsCfg,
    MmTemplatesCfg,
    NotificationsCfg,
    RepositoryCfg,
)
from virtual_dev.infrastructure.config.schema import EscalationCfg
from virtual_dev.infrastructure.db import PlanRow, TaskRow
from virtual_dev.infrastructure.db.base import session_scope


# ============================================================
#                      In-memory fakes
# ============================================================


class _RecordingChat(ChatPort):
    def __init__(self) -> None:
        self.sent_dms: list[tuple[str, str]] = []
        self.sent_channels: list[tuple[str, str, str | None]] = []
        self._counter = 0

    async def read_thread(self, thread_root_id: str) -> Sequence[ChatMessage]:
        return []

    async def send_direct(self, user_id: str, text: str) -> ChatMessage:
        self.sent_dms.append((user_id, text))
        self._counter += 1
        return ChatMessage(
            id=f"post-{self._counter}", channel_id=f"dm-{user_id}",
            author_id="bot", text=text,
            timestamp=datetime.now(timezone.utc), trusted=True,
        )

    async def send_to_channel(
        self, channel_id: str, text: str, thread_root_id: str | None = None,
    ) -> ChatMessage:
        self.sent_channels.append((channel_id, text, thread_root_id))
        self._counter += 1
        return ChatMessage(
            id=f"post-{self._counter}", channel_id=channel_id,
            author_id="bot", text=text,
            timestamp=datetime.now(timezone.utc), trusted=True,
            thread_root_id=thread_root_id,
        )

    async def find_user_by_email(self, email: str) -> ChatUser | None:
        return ChatUser(id=f"uid-{email}", username=email.split("@")[0], email=email)

    async def find_user_by_username(self, username: str) -> ChatUser | None:
        if username == "ghost":
            return None
        return ChatUser(id=f"uid-{username}", username=username)

    async def add_reaction(self, post_id: str, emoji_name: str) -> None:
        return None

    async def get_post(self, post_id: str) -> ChatMessage | None:
        return None

    def subscribe(self) -> AsyncIterator[ChatMessage]:
        raise NotImplementedError


class _CapturingBus(MessageBusPort):
    def __init__(self) -> None:
        self.published: list[AgentMessage] = []

    async def publish(self, message: AgentMessage) -> None:
        self.published.append(message)

    async def subscribe(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


class _ScriptedClassifier:
    """Returns canned ClassificationResult per call."""

    def __init__(self, results: list[ClassificationResult]) -> None:
        self._results = list(results)
        self.calls = 0

    async def classify(self, **kwargs: Any) -> ClassificationResult:
        self.calls += 1
        if not self._results:
            raise RuntimeError("no more scripted results")
        return self._results.pop(0)


class _ScriptedCounterAnswerer:
    def __init__(self, results: list[CounterAnswerResult]) -> None:
        self._results = list(results)
        self.calls = 0

    async def answer(self, **kwargs: Any) -> CounterAnswerResult:
        self.calls += 1
        if not self._results:
            raise RuntimeError("no more scripted counter results")
        return self._results.pop(0)


class _DeterministicResolver(StakeholderResolver):
    """Skips the LLM step. Just runs the deterministic prefixes."""

    async def resolve(self, raw_hint, context=None):  # type: ignore[no-untyped-def]
        # Reuse the parent's deterministic head; if it returns
        # UNRESOLVED_NAME (LLM step), we keep it that way (no LLM call).
        from virtual_dev.application.services.clarification.stakeholder_resolver import (
            _EMAIL_RE,
            _USERNAME_RE,
        )
        hint = (raw_hint or "").strip().lstrip("@").strip()
        if not hint:
            return Stakeholder(kind=StakeholderKind.UNRESOLVED_NAME, raw_hint="")
        if _EMAIL_RE.match(hint):
            user_id = await self._communicator.resolve_user_id(email=hint)
            if user_id is not None:
                return Stakeholder(
                    kind=StakeholderKind.EMAIL, raw_hint=raw_hint,
                    resolved_mm_user_id=user_id, display_name=hint,
                )
        if _USERNAME_RE.match(hint):
            user_id = await self._communicator.resolve_user_id(username=hint)
            if user_id is not None:
                return Stakeholder(
                    kind=StakeholderKind.EXPLICIT_HANDLE, raw_hint=raw_hint,
                    resolved_mm_user_id=user_id, display_name=hint,
                )
        return Stakeholder(
            kind=StakeholderKind.UNRESOLVED_NAME, raw_hint=raw_hint,
        )


# ============================================================
#                  Builders / fixtures
# ============================================================


def _cfg(*, max_chain_depth: int = 4, max_subqs: int = 10) -> AppConfig:
    cfg = AppConfig(
        repositories=[RepositoryCfg(key="bellingshausen", url="git@x:g/b.git")],
        agents=AgentsCfg(escalation=EscalationCfg(mattermost_user="tech-lead")),
        mappings=MappingsCfg(),
        notifications=NotificationsCfg(mattermost=MmTemplatesCfg(
            clarifier_question="Q: {question} (ticket {external_id})",
            clarifier_answer_ack="спасибо!",
            clarifier_redirect_ack="перенаправил на @{handle}",
            clarifier_handle_request="дай ник {raw_name} для: {original_question}",
            clarifier_counter_factual_intro="контекст: {bot_answer}",
            clarifier_out_of_scope_ack="ok",
            clarifier_dont_know_ack="ok dk",
            clarifier_escalation_to_lead=(
                "stuck on {external_id}: {original_question}; "
                "chain={chain_summary}; reason={reason}"
            ),
        )),
    )
    cfg.agents.clarification.max_chain_depth = max_chain_depth
    cfg.agents.clarification.max_subquestions_per_root = max_subqs
    cfg.agents.clarification.coalesce_window_seconds = 1   # short for tests
    return cfg


async def _seed_task_and_plan(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    external_id: str = "DM-1",
    description: str = "Нужна ручка World, спросить @alice",
) -> tuple[TaskRow, int]:
    async with session_scope(session_factory) as session:
        task = TaskRow(
            tracker="jira", external_id=external_id, title="Test",
            description=description, url=f"https://jira/{external_id}",
            internal_status="planning", reporter_id="reporter.user",
        )
        session.add(task)
        await session.flush()
        plan = PlanRow(
            tracker="jira", task_external_id=external_id,
            summary="plan", status=PlanStatus.CLARIFYING.value,
            target_repo_key="bellingshausen", model="m", agent_key="analyst",
        )
        session.add(plan)
        await session.flush()
        return task, plan.id


def _orchestrator(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    chat: _RecordingChat,
    classifier: _ScriptedClassifier,
    counter: _ScriptedCounterAnswerer,
    bus: _CapturingBus | None = None,
    config: AppConfig | None = None,
) -> ClarificationOrchestrator:
    cfg = config or _cfg()
    communicator = CommunicatorService(
        chat, InjectionFilter(), respect_working_hours=False,
    )
    resolver = _DeterministicResolver(
        communicator=communicator,
        code_agent=None,                     # type: ignore[arg-type]
        config=cfg,
        prompts_loader=None,                 # type: ignore[arg-type]
    )
    return ClarificationOrchestrator(
        repo=QuestionRepository(session_factory),
        communicator=communicator,
        classifier=classifier,               # type: ignore[arg-type]
        counter_answerer=counter,            # type: ignore[arg-type]
        stakeholder_resolver=resolver,
        config=cfg,
        session_factory=session_factory,
        message_bus=bus,
    )


def _plan_with_questions(*qs: OpenQuestion) -> Plan:
    return Plan(
        task_external_id="DM-1", tracker="jira", summary="x",
        steps=[], open_questions=list(qs),
        status=PlanStatus.CLARIFYING,
    )


# ============================================================
#                       Tests
# ============================================================


@pytest.mark.asyncio
async def test_request_clarifications_dms_each_question(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task, plan_id = await _seed_task_and_plan(session_factory)
    chat = _RecordingChat()
    orch = _orchestrator(
        session_factory, chat=chat,
        classifier=_ScriptedClassifier([]),
        counter=_ScriptedCounterAnswerer([]),
    )
    plan = _plan_with_questions(
        OpenQuestion(question="Как называется ручка?", ask_whom="alice"),
        OpenQuestion(question="Какой ответ?", ask_whom="bob@2gis.ru"),
    )
    sent = await orch.request_clarifications(
        task_row=task, plan=plan, plan_row_id=plan_id,
    )
    assert sent == 2
    assert {dm[0] for dm in chat.sent_dms} == {"uid-alice", "uid-bob@2gis.ru"}


@pytest.mark.asyncio
async def test_direct_classification_settles_root_and_re_dispatches(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task, plan_id = await _seed_task_and_plan(session_factory)
    chat = _RecordingChat()
    bus = _CapturingBus()
    classifier = _ScriptedClassifier([
        ClassificationResult(
            classification=Classification.DIRECT,
            reasoning="clear answer",
            extracted={"direct_answer_text": "UserAPI"},
        ),
    ])
    orch = _orchestrator(
        session_factory, chat=chat, classifier=classifier,
        counter=_ScriptedCounterAnswerer([]), bus=bus,
    )
    plan = _plan_with_questions(
        OpenQuestion(question="Q1", ask_whom="alice"),
    )
    await orch.request_clarifications(
        task_row=task, plan=plan, plan_row_id=plan_id,
    )

    # Pull the question to grab its asked_post_id.
    questions = await orch._repo.list_for_task("jira", "DM-1")
    q = questions[0]

    # Append a fragment + flush.
    fragment = ChatMessage(
        id="m1", channel_id=q.mm_channel_id or "dm-uid-alice",
        author_id="uid-alice", text="UserAPI",
        timestamp=datetime.now(timezone.utc), trusted=False,
        thread_root_id=q.asked_post_id,
    )
    await orch.append_fragment(q.id, fragment)
    # Force the idle window: monkey-patch the question's last_fragment_at.
    async with session_scope(session_factory) as session:
        from virtual_dev.infrastructure.db import QuestionRow
        from datetime import timedelta as _td
        row = (await session.execute(
            select(QuestionRow).where(QuestionRow.id == q.id)
        )).scalar_one()
        row.last_fragment_at = datetime.now(timezone.utc) - _td(seconds=10)

    flushed = await orch.flush_idle()
    assert flushed == 1

    settled = await orch._repo.get(q.id)
    assert settled is not None
    assert settled.state is QuestionState.ANSWERED

    # Bus got task.discovered.
    assert any(m.topic == "task.discovered" for m in bus.published)


@pytest.mark.asyncio
async def test_redirect_with_resolved_handle_spawns_child(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task, plan_id = await _seed_task_and_plan(session_factory)
    chat = _RecordingChat()
    classifier = _ScriptedClassifier([
        ClassificationResult(
            classification=Classification.REDIRECT,
            reasoning="r",
            extracted={"redirect_target_handle": "bob"},
        ),
    ])
    orch = _orchestrator(
        session_factory, chat=chat, classifier=classifier,
        counter=_ScriptedCounterAnswerer([]),
    )
    plan = _plan_with_questions(
        OpenQuestion(question="Q1", ask_whom="alice"),
    )
    await orch.request_clarifications(
        task_row=task, plan=plan, plan_row_id=plan_id,
    )
    questions = await orch._repo.list_for_task("jira", "DM-1")
    parent = questions[0]

    fragment = ChatMessage(
        id="m1", channel_id=parent.mm_channel_id or "x",
        author_id="uid-alice", text="спроси @bob",
        timestamp=datetime.now(timezone.utc), trusted=False,
    )
    await orch.append_fragment(parent.id, fragment)
    async with session_scope(session_factory) as session:
        from virtual_dev.infrastructure.db import QuestionRow
        from datetime import timedelta as _td
        row = (await session.execute(
            select(QuestionRow).where(QuestionRow.id == parent.id)
        )).scalar_one()
        row.last_fragment_at = datetime.now(timezone.utc) - _td(seconds=10)

    await orch.flush_idle()

    questions_after = await orch._repo.list_for_task("jira", "DM-1")
    assert len(questions_after) == 2
    parent_after = next(q for q in questions_after if q.id == parent.id)
    child = next(q for q in questions_after if q.id != parent.id)
    assert parent_after.state is QuestionState.REDIRECTED
    assert child.parent_id == parent.id
    assert child.chain_depth == 1
    assert child.stakeholder.resolved_mm_user_id == "uid-bob"
    # Bot DM'd bob.
    assert any(uid == "uid-bob" for uid, _ in chat.sent_dms)


@pytest.mark.asyncio
async def test_max_chain_depth_guard_escalates_instead_of_spawning(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Set max_chain_depth=1, one redirect = ok, second would hit guard."""
    task, plan_id = await _seed_task_and_plan(session_factory)
    chat = _RecordingChat()
    cfg = _cfg(max_chain_depth=1)
    classifier = _ScriptedClassifier([
        ClassificationResult(
            classification=Classification.REDIRECT,
            reasoning="r1",
            extracted={"redirect_target_handle": "bob"},
        ),
        ClassificationResult(
            classification=Classification.REDIRECT,
            reasoning="r2",
            extracted={"redirect_target_handle": "carol"},
        ),
    ])
    orch = _orchestrator(
        session_factory, chat=chat, classifier=classifier,
        counter=_ScriptedCounterAnswerer([]), config=cfg,
    )
    plan = _plan_with_questions(OpenQuestion(question="Q", ask_whom="alice"))
    await orch.request_clarifications(
        task_row=task, plan=plan, plan_row_id=plan_id,
    )
    qs = await orch._repo.list_for_task("jira", "DM-1")
    parent = qs[0]
    await orch.append_fragment(parent.id, _msg("m1", "спроси @bob"))
    await _force_idle(session_factory, parent.id)
    await orch.flush_idle()
    # Now we have parent (REDIRECTED) + child (ASKING with depth=1).
    qs2 = await orch._repo.list_for_task("jira", "DM-1")
    child = max(qs2, key=lambda q: q.chain_depth)
    assert child.chain_depth == 1

    # Second-redirect attempt: child's reply, which the classifier
    # tries to redirect again. Guard should trip.
    await orch.append_fragment(child.id, _msg("m2", "спроси @carol"))
    await _force_idle(session_factory, child.id)
    await orch.flush_idle()

    qs3 = await orch._repo.list_for_task("jira", "DM-1")
    # No new node spawned (still 2 questions).
    assert len(qs3) == 2
    child_after = next(q for q in qs3 if q.id == child.id)
    assert child_after.state is QuestionState.ESCALATED
    # Lead got a DM.
    assert any(uid == "uid-tech-lead" for uid, _ in chat.sent_dms)


@pytest.mark.asyncio
async def test_dont_know_escalates_to_team_lead(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task, plan_id = await _seed_task_and_plan(session_factory)
    chat = _RecordingChat()
    classifier = _ScriptedClassifier([
        ClassificationResult(
            classification=Classification.DONT_KNOW,
            reasoning="honest",
            extracted={},
        ),
    ])
    orch = _orchestrator(
        session_factory, chat=chat, classifier=classifier,
        counter=_ScriptedCounterAnswerer([]),
    )
    plan = _plan_with_questions(OpenQuestion(question="Q", ask_whom="alice"))
    await orch.request_clarifications(
        task_row=task, plan=plan, plan_row_id=plan_id,
    )
    qs = await orch._repo.list_for_task("jira", "DM-1")
    q = qs[0]
    await orch.append_fragment(q.id, _msg("m1", "не знаю"))
    await _force_idle(session_factory, q.id)
    await orch.flush_idle()

    settled = await orch._repo.get(q.id)
    assert settled is not None
    assert settled.state is QuestionState.ESCALATED
    assert any(uid == "uid-tech-lead" for uid, _ in chat.sent_dms)


@pytest.mark.asyncio
async def test_counter_question_factual_bot_self_answers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task, plan_id = await _seed_task_and_plan(session_factory)
    chat = _RecordingChat()
    classifier = _ScriptedClassifier([
        ClassificationResult(
            classification=Classification.COUNTER_QUESTION,
            reasoning="r",
            extracted={
                "counter_question_text": "какая из 10 ручек?",
                "counter_question_reasoning": "нужна конкретика",
                "counter_question_kind": "factual",
            },
        ),
    ])
    counter = _ScriptedCounterAnswerer([
        CounterAnswerResult(
            answer_text="Имеется в виду /api/v2/users.",
            confidence=0.9,
            escalate_to_reporter=False,
            reasoning="found in code",
        ),
    ])
    orch = _orchestrator(
        session_factory, chat=chat, classifier=classifier,
        counter=counter,
    )
    plan = _plan_with_questions(OpenQuestion(question="Q", ask_whom="alice"))
    await orch.request_clarifications(
        task_row=task, plan=plan, plan_row_id=plan_id,
    )
    qs = await orch._repo.list_for_task("jira", "DM-1")
    parent = qs[0]
    await orch.append_fragment(parent.id, _msg("m1", "какая из 10?"))
    await _force_idle(session_factory, parent.id)
    await orch.flush_idle()

    parent_after = await orch._repo.get(parent.id)
    assert parent_after is not None
    # Bot self-answered → parent goes back to ASKING.
    assert parent_after.state is QuestionState.ASKING
    # Channel reply with the bot's contextual answer.
    assert any(
        "Имеется в виду /api/v2/users." in text
        for _, text, _ in chat.sent_channels
    )


@pytest.mark.asyncio
async def test_counter_question_business_escalates_to_reporter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task, plan_id = await _seed_task_and_plan(session_factory)
    chat = _RecordingChat()
    classifier = _ScriptedClassifier([
        ClassificationResult(
            classification=Classification.COUNTER_QUESTION,
            reasoning="r",
            extracted={
                "counter_question_text": "что важнее — скорость или точность?",
                "counter_question_reasoning": "нужно решение product",
                "counter_question_kind": "business",
            },
        ),
    ])
    orch = _orchestrator(
        session_factory, chat=chat, classifier=classifier,
        counter=_ScriptedCounterAnswerer([]),
    )
    plan = _plan_with_questions(OpenQuestion(question="Q", ask_whom="alice"))
    await orch.request_clarifications(
        task_row=task, plan=plan, plan_row_id=plan_id,
    )
    qs = await orch._repo.list_for_task("jira", "DM-1")
    parent = qs[0]
    await orch.append_fragment(parent.id, _msg("m1", "что важнее?"))
    await _force_idle(session_factory, parent.id)
    await orch.flush_idle()

    qs2 = await orch._repo.list_for_task("jira", "DM-1")
    assert len(qs2) == 2
    child = next(q for q in qs2 if q.id != parent.id)
    assert child.stakeholder.kind is StakeholderKind.TASK_AUTHOR
    assert child.stakeholder.resolved_mm_user_id == "uid-reporter.user"


@pytest.mark.asyncio
async def test_redirect_unresolvable_name_spawns_handle_request(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Free-form name → bot DMs original respondent for the handle."""
    task, plan_id = await _seed_task_and_plan(session_factory)
    chat = _RecordingChat()
    classifier = _ScriptedClassifier([
        ClassificationResult(
            classification=Classification.REDIRECT,
            reasoning="r",
            extracted={"redirect_target_name": "Вася Курочкин"},
        ),
    ])
    orch = _orchestrator(
        session_factory, chat=chat, classifier=classifier,
        counter=_ScriptedCounterAnswerer([]),
    )
    plan = _plan_with_questions(OpenQuestion(question="Q", ask_whom="alice"))
    await orch.request_clarifications(
        task_row=task, plan=plan, plan_row_id=plan_id,
    )
    qs = await orch._repo.list_for_task("jira", "DM-1")
    parent = qs[0]
    await orch.append_fragment(parent.id, _msg("m1", "спроси у Васи Курочкина"))
    await _force_idle(session_factory, parent.id)
    await orch.flush_idle()

    parent_after = await orch._repo.get(parent.id)
    assert parent_after is not None
    assert parent_after.state is QuestionState.ASKING_FOR_STAKEHOLDER

    qs2 = await orch._repo.list_for_task("jira", "DM-1")
    assert len(qs2) == 2
    child = next(q for q in qs2 if q.id != parent.id)
    # Child question text is the handle-request template.
    assert "Вася Курочкин" in child.text or "ник" in child.text
    # Child's stakeholder is the SAME human (alice) — we asked them
    # to clarify whom they meant.
    assert child.stakeholder.resolved_mm_user_id == "uid-alice"


@pytest.mark.asyncio
async def test_deadline_sweep_abandons_overdue_questions(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task, plan_id = await _seed_task_and_plan(session_factory)
    chat = _RecordingChat()
    orch = _orchestrator(
        session_factory, chat=chat,
        classifier=_ScriptedClassifier([]),
        counter=_ScriptedCounterAnswerer([]),
    )
    plan = _plan_with_questions(OpenQuestion(question="Q", ask_whom="alice"))
    await orch.request_clarifications(
        task_row=task, plan=plan, plan_row_id=plan_id,
    )
    # Force deadline into the past.
    qs = await orch._repo.list_for_task("jira", "DM-1")
    q = qs[0]
    async with session_scope(session_factory) as session:
        from virtual_dev.infrastructure.db import QuestionRow
        from datetime import timedelta as _td
        row = (await session.execute(
            select(QuestionRow).where(QuestionRow.id == q.id)
        )).scalar_one()
        row.deadline_at = datetime.now(timezone.utc) - _td(hours=1)

    swept = await orch.sweep_deadlines()
    assert swept == 1
    after = await orch._repo.get(q.id)
    assert after is not None
    assert after.state is QuestionState.ABANDONED


@pytest.mark.asyncio
async def test_out_of_scope_escalates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task, plan_id = await _seed_task_and_plan(session_factory)
    chat = _RecordingChat()
    classifier = _ScriptedClassifier([
        ClassificationResult(
            classification=Classification.OUT_OF_SCOPE,
            reasoning="hostile",
            extracted={"out_of_scope_kind": OutOfScopeKind.ABUSE.value},
        ),
    ])
    orch = _orchestrator(
        session_factory, chat=chat, classifier=classifier,
        counter=_ScriptedCounterAnswerer([]),
    )
    plan = _plan_with_questions(OpenQuestion(question="Q", ask_whom="alice"))
    await orch.request_clarifications(
        task_row=task, plan=plan, plan_row_id=plan_id,
    )
    qs = await orch._repo.list_for_task("jira", "DM-1")
    q = qs[0]
    await orch.append_fragment(q.id, _msg("m1", "иди отсюда"))
    await _force_idle(session_factory, q.id)
    await orch.flush_idle()

    after = await orch._repo.get(q.id)
    assert after is not None
    assert after.state is QuestionState.ESCALATED


# ============================================================
#                    helpers
# ============================================================


def _msg(post_id: str, text: str) -> ChatMessage:
    return ChatMessage(
        id=post_id, channel_id="dm", author_id="uid-alice",
        text=text, timestamp=datetime.now(timezone.utc), trusted=False,
    )


async def _force_idle(
    session_factory: async_sessionmaker[AsyncSession], question_id: int,
) -> None:
    """Backdate last_fragment_at so coalescer's window has elapsed."""
    from virtual_dev.infrastructure.db import QuestionRow
    from datetime import timedelta as _td
    async with session_scope(session_factory) as session:
        row = (await session.execute(
            select(QuestionRow).where(QuestionRow.id == question_id)
        )).scalar_one()
        row.last_fragment_at = datetime.now(timezone.utc) - _td(seconds=600)
