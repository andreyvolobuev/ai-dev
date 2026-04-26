"""GoalOrchestrator state-machine tests.

Covers the six scenarios called out in the Phase 3.9 plan:

  1. «Vasya regression» — planner re-composes the question for a new
     recipient instead of copy-pasting the previous text. Closes the
     bug that drove the rewrite.
  2. Stale-fragment-during-REPLANNING — fragments that arrive while the
     planner is mid-call must be archived (not coalesced) when the
     planner emits a brand-new ASK.
  3. Counter-Q factual self-answer — planner answers from the repo
     without DM-ing anyone.
  4. ``no_duplicate_target`` guard — same handle + dedupe_key twice in a
     row → ESCALATED, lead-DM sent.
  5. REPLANNING crash recovery — sweep reverts stuck REPLANNING goals to
     READY_TO_REPLAN.
  6. ``wait_for_human`` — planner defers, sweep wakes goal once
     ``next_planner_run_at`` elapses.
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
from virtual_dev.application.services.communicator import (
    CommunicatorService,
)
from virtual_dev.application.services.injection_filter import InjectionFilter
from virtual_dev.domain.models.chat import ChatMessage, ChatUser
from virtual_dev.domain.models.clarification_goal import (
    ClarificationGoal,
    GoalState,
    GoalStepKind,
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
)
from virtual_dev.infrastructure.db import GoalRow, PlanRow, TaskRow
from virtual_dev.infrastructure.db.base import session_scope

# ============================================================
#                          Fakes
# ============================================================


class _FakeChat(ChatPort):
    """ChatPort fake — records sent DMs, lets us script user lookups."""

    def __init__(self, *, users: dict[str, str] | None = None) -> None:
        # users maps username → mm-user-id
        self._users = users or {}
        self.sent_dms: list[tuple[str, str]] = []   # (user_id, text)
        self.sent_channels: list[tuple[str, str]] = []
        self.reactions: list[tuple[str, str]] = []
        self._post_seq = 0

    def _next_post_id(self) -> str:
        self._post_seq += 1
        return f"bot-post-{self._post_seq}"

    async def read_thread(self, thread_root_id: str) -> Sequence[ChatMessage]:
        return []

    async def send_direct(self, user_id: str, text: str) -> ChatMessage:
        self.sent_dms.append((user_id, text))
        return ChatMessage(
            id=self._next_post_id(),
            channel_id=f"dm-{user_id}",
            author_id="uid-bot",
            text=text,
            timestamp=datetime.now(timezone.utc),
            trusted=True,
        )

    async def send_to_channel(
        self, channel_id: str, text: str, thread_root_id: str | None = None,
    ) -> ChatMessage:
        self.sent_channels.append((channel_id, text))
        return ChatMessage(
            id=self._next_post_id(),
            channel_id=channel_id, author_id="uid-bot", text=text,
            timestamp=datetime.now(timezone.utc), trusted=True,
            thread_root_id=thread_root_id,
        )

    async def find_user_by_email(self, email: str) -> ChatUser | None:
        for username, uid in self._users.items():
            if username == email.split("@", 1)[0]:
                return ChatUser(id=uid, username=username, email=email)
        return None

    async def find_user_by_username(self, username: str) -> ChatUser | None:
        uid = self._users.get(username)
        if uid is None:
            return None
        return ChatUser(id=uid, username=username)

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
    """Returns scripted PlannerDecisions in order."""

    def __init__(self, decisions: list[PlannerDecision]) -> None:
        self._decisions = list(decisions)
        self.calls: list[Any] = []

    async def decide(self, inp: Any) -> PlannerDecision:
        self.calls.append(inp)
        if not self._decisions:
            raise AssertionError("planner called more times than scripted")
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


def _config(*, escalation_user: str = "lead", max_planner_calls: int = 8) -> AppConfig:
    return AppConfig(
        repositories=[RepositoryCfg(key="x", url="git@x:x.git")],
        agents=AgentsCfg(
            escalation=EscalationCfg(mattermost_user=escalation_user),
            clarification=ClarificationCfg(
                coalesce_window_seconds=60,
                poll_interval_seconds=10,
                max_planner_calls_per_goal=max_planner_calls,
                max_goal_age_hours=48,
                send_retry_max=3,
                replanning_stuck_after_minutes=10,
            ),
        ),
        mappings=MappingsCfg(),
        notifications=NotificationsCfg(mattermost=MmTemplatesCfg()),
    )


def _make_orchestrator(
    session_factory: async_sessionmaker[AsyncSession],
    chat: _FakeChat,
    planner: _ScriptedPlanner,
    *,
    config: AppConfig | None = None,
    bus: MessageBusPort | None = None,
) -> GoalOrchestrator:
    cfg = config or _config()
    communicator = CommunicatorService(
        chat, InjectionFilter(), respect_working_hours=False,
    )
    return GoalOrchestrator(
        repo=GoalRepository(session_factory),
        communicator=communicator,
        planner=planner,                         # type: ignore[arg-type]
        config=cfg,
        session_factory=session_factory,
        message_bus=bus,
    )


async def _seed_goal(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    description: str = "получить пример body",
    why_it_matters: str = "нужно для repro",
    contact_hint: str = "team-lead",
    coalesce_window_seconds: int = 60,
) -> ClarificationGoal:
    repo = GoalRepository(session_factory)
    return await repo.create_goal(
        plan_id=1,
        tracker="jira", task_external_id="DM-1",
        description=description,
        why_it_matters=why_it_matters,
        initial_contact_hint=contact_hint,
        coalesce_window_seconds=coalesce_window_seconds,
        deadline_at=datetime.now(timezone.utc) + timedelta(hours=48),
    )


async def _seed_task_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Tasks/plans need to exist for _send_lead_escalation -> _task_url."""
    async with session_scope(session_factory) as session:
        session.add(TaskRow(
            tracker="jira", external_id="DM-1",
            title="Test", description="...",
            url="https://jira/DM-1",
        ))
        session.add(PlanRow(
            tracker="jira", task_external_id="DM-1",
            summary="...", target_repo_key=None,
        ))


# ============================================================
#                  1. Vasya regression
# ============================================================


@pytest.mark.asyncio
async def test_vasya_regression_planner_recomposes_for_new_recipient(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The bug: bot got handle ``@v.kura`` from team-lead, then DM'd
    Vasya with the SAME text ('what's Vasya's MM handle?') — losing the
    actual goal (body example).

    The fix: planner sees the goal description AND the chain, and
    re-composes a recipient-appropriate message. The orchestrator
    forwards the planner's verbatim ``message`` text to Communicator.
    """
    await _seed_task_row(session_factory)
    chat = _FakeChat(users={"v.kura": "uid-vkura"})

    # Scripted: planner emits an ASK to v.kura with a body-question text
    # (not the previous "what's the handle" text).
    decision = PlannerDecision(
        action=PlannerActionKind.ASK,
        reasoning="Now have v.kura's handle; ask about body example.",
        to_handle="v.kura",
        message="Привет! Подскажи пример request body для DM-1, нужно для воспроизведения.",
        dedupe_key="vkura:body-example",
    )
    planner = _ScriptedPlanner([decision])

    # Simulate the prior chain: bot already asked team-lead. Reply has
    # been buffered as a fragment but not yet coalesced into a step.
    goal = await _seed_goal(session_factory)
    repo = GoalRepository(session_factory)
    await repo.update_state(
        goal.id, GoalState.AWAITING_REPLY,
        outstanding_post_id="post-1",
        outstanding_user_id="uid-lead",
        outstanding_username="team-lead",
        outstanding_channel="dm-lead",
        outstanding_text="Кто ответит про body для DM-1?",
        outstanding_dedupe_key="lead:who-knows-body",
    )
    await repo.append_step(
        goal_id=goal.id, kind=GoalStepKind.BOT_ASKED,
        text="Кто ответит про body для DM-1?",
        target_username="team-lead", target_user_id="uid-lead",
        metadata={"asked_post_id": "post-1", "dedupe_key": "lead:who-knows-body"},
    )
    await repo.append_fragment(
        goal_id=goal.id, mm_post_id="reply-1",
        asked_post_id="post-1",
        text="ask Vasya, @v.kura",
        received_at=datetime.now(timezone.utc),
    )

    orch = _make_orchestrator(session_factory, chat, planner)
    # Drive the planner via flush_idle (claim_for_replan picks up
    # COALESCING goals).
    refreshed = await repo.get(goal.id)
    assert refreshed is not None
    claimed = await repo.claim_for_replan(refreshed.id)
    assert claimed is not None
    await orch._replan_after_reply(claimed)  # noqa: SLF001 — drive directly, skip idle wait

    assert len(chat.sent_dms) == 1
    target_uid, sent_text = chat.sent_dms[0]
    assert target_uid == "uid-vkura"

    # Intent of original goal must be preserved.
    assert "body" in sent_text.lower() or "пример" in sent_text.lower()
    # Must NOT be a copy of the previous question (which asked who knows
    # the answer / the MM handle).
    assert "ник" not in sent_text.lower() and "handle" not in sent_text.lower()

    # State now AWAITING_REPLY on the new recipient.
    refreshed = await repo.get(goal.id)
    assert refreshed is not None
    assert refreshed.state == GoalState.AWAITING_REPLY
    assert refreshed.current_target_user_id == "uid-vkura"


# ============================================================
#         2. Stale-fragment-during-REPLANNING
# ============================================================


@pytest.mark.asyncio
async def test_stale_fragments_archived_when_planner_starts_new_ask(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """While REPLANNING, late fragments accumulate. If the planner then
    issues a new ASK, those fragments are archived (audit trail) so the
    next planner-call doesn't see them as evidence about the new ask.
    """
    await _seed_task_row(session_factory)
    chat = _FakeChat(users={"alice": "uid-alice"})
    repo = GoalRepository(session_factory)
    goal = await _seed_goal(session_factory)

    # Goal already had an outstanding ask to alice; simulate a coalesced
    # reply that triggered REPLANNING.
    await repo.update_state(
        goal.id, GoalState.AWAITING_REPLY,
        outstanding_post_id="post-q1", outstanding_user_id="uid-bob",
        outstanding_username="bob", outstanding_channel="dm-bob",
        outstanding_text="первый вопрос",
    )
    await repo.append_step(
        goal_id=goal.id, kind=GoalStepKind.BOT_ASKED,
        text="первый вопрос", target_username="bob", target_user_id="uid-bob",
    )
    await repo.append_step(
        goal_id=goal.id, kind=GoalStepKind.HUMAN_REPLIED,
        text="спроси Алису", target_username="bob", target_user_id="uid-bob",
    )

    # Two late fragments arrive while REPLANNING.
    await repo.update_state(goal.id, GoalState.REPLANNING)
    now = datetime.now(timezone.utc)
    await repo.append_fragment(
        goal_id=goal.id, mm_post_id="late-1", asked_post_id="post-q1",
        text="ой, а ещё", received_at=now,
    )
    await repo.append_fragment(
        goal_id=goal.id, mm_post_id="late-2", asked_post_id="post-q1",
        text="и вот это", received_at=now + timedelta(seconds=1),
    )
    pending = await repo.list_unflushed_fragments(goal.id)
    assert len(pending) == 2

    # Planner now issues a new ASK to alice.
    decision = PlannerDecision(
        action=PlannerActionKind.ASK,
        reasoning="redirect",
        to_handle="alice",
        message="Привет, Алиса! Подскажи про body.",
        dedupe_key="alice:body",
    )
    orch = _make_orchestrator(session_factory, chat, _ScriptedPlanner([]))
    refreshed = await repo.get(goal.id)
    assert refreshed is not None
    await orch._apply_decision(refreshed, decision)  # noqa: SLF001

    # After dispatching the ASK, the previous-question fragments must be
    # archived (no longer in the buffer; present as STALE_FRAGMENT steps).
    pending = await repo.list_unflushed_fragments(goal.id)
    assert pending == []
    steps = await repo.list_steps(goal.id)
    stale_texts = [s.text for s in steps if s.kind == GoalStepKind.STALE_FRAGMENT]
    assert "ой, а ещё" in stale_texts
    assert "и вот это" in stale_texts

    # New ASK is now outstanding to alice.
    refreshed = await repo.get(goal.id)
    assert refreshed is not None
    assert refreshed.current_target_user_id == "uid-alice"


# ============================================================
#       3. Counter-Q factual self-answer (no DM)
# ============================================================


@pytest.mark.asyncio
async def test_planner_can_achieve_without_dm(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """If the planner answers from the repo (Read/Grep/Researcher) and
    returns ``achieve``, no DM is sent and state goes straight to ACHIEVED.
    """
    await _seed_task_row(session_factory)
    chat = _FakeChat()
    planner = _ScriptedPlanner([PlannerDecision(
        action=PlannerActionKind.ACHIEVE,
        reasoning="Found endpoint in src/api.py",
        final_answer="POST /api/v1/tasks",
        confidence=0.9,
    )])
    bus = _RecordingBus()
    orch = _make_orchestrator(session_factory, chat, planner, bus=bus)

    plan = Plan(
        task_external_id="DM-1", tracker="jira",
        summary="x",
        open_questions=[OpenQuestion(question="нужен endpoint", why_it_matters="")],
        status=PlanStatus.CLARIFYING,
    )
    async with session_scope(session_factory) as session:
        task = (await session.execute(
            select(TaskRow).where(TaskRow.external_id == "DM-1")
        )).scalar_one()
        plan_row = (await session.execute(
            select(PlanRow).where(PlanRow.task_external_id == "DM-1")
        )).scalar_one()
    await orch.request_clarifications(
        task_row=task, plan=plan, plan_row_id=plan_row.id,
    )

    # No DM, goal in ACHIEVED, task.discovered re-published.
    assert chat.sent_dms == []
    repo = GoalRepository(session_factory)
    goals = await repo.list_for_plan(plan_row.id)
    assert len(goals) == 1
    assert goals[0].state == GoalState.ACHIEVED
    assert goals[0].final_answer == "POST /api/v1/tasks"
    assert any(m.topic == "task.discovered" for m in bus.published)


# ============================================================
#       4. no_duplicate_target loop guard
# ============================================================


@pytest.mark.asyncio
async def test_duplicate_ask_to_same_handle_and_dedupe_escalates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Planner mistakenly returns identical ASK twice: same to_handle,
    same dedupe_key, no human reply between. Second ASK is rejected and
    the goal is ESCALATED (lead-DM sent).
    """
    await _seed_task_row(session_factory)
    chat = _FakeChat(users={"alice": "uid-alice", "lead": "uid-lead"})
    decisions = [
        PlannerDecision(
            action=PlannerActionKind.ASK, reasoning="first",
            to_handle="alice", message="Q1", dedupe_key="alice:body",
        ),
        PlannerDecision(
            action=PlannerActionKind.ASK, reasoning="second (same target!)",
            to_handle="alice", message="Q2", dedupe_key="alice:body",
        ),
    ]
    planner = _ScriptedPlanner(decisions)
    orch = _make_orchestrator(session_factory, chat, planner)

    repo = GoalRepository(session_factory)
    goal = await _seed_goal(session_factory)

    # First ASK lands.
    await orch._invoke_planner(goal)  # noqa: SLF001
    after_first = await repo.get(goal.id)
    assert after_first is not None
    assert after_first.state == GoalState.AWAITING_REPLY
    assert chat.sent_dms[0][0] == "uid-alice"

    # Now planner is "consulted again" without a human reply between —
    # second ASK must be detected and rejected.
    await orch._invoke_planner(after_first)  # noqa: SLF001

    # Goal is escalated.
    after_second = await repo.get(goal.id)
    assert after_second is not None
    assert after_second.state == GoalState.ESCALATED
    # Lead got the escalation DM.
    lead_dms = [(uid, txt) for uid, txt in chat.sent_dms if uid == "uid-lead"]
    assert len(lead_dms) == 1
    assert "DM-1" in lead_dms[0][1]


@pytest.mark.asyncio
async def test_repeat_ask_after_human_reply_is_allowed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Same handle + dedupe_key is fine if a HUMAN_REPLIED step lies
    between — the human gave new evidence."""
    await _seed_task_row(session_factory)
    chat = _FakeChat(users={"alice": "uid-alice"})
    decisions = [
        PlannerDecision(
            action=PlannerActionKind.ASK, reasoning="first",
            to_handle="alice", message="Q1", dedupe_key="alice:body",
        ),
        PlannerDecision(
            action=PlannerActionKind.ASK, reasoning="follow-up after reply",
            to_handle="alice", message="Q1-followup", dedupe_key="alice:body",
        ),
    ]
    planner = _ScriptedPlanner(decisions)
    orch = _make_orchestrator(session_factory, chat, planner)
    repo = GoalRepository(session_factory)
    goal = await _seed_goal(session_factory)

    await orch._invoke_planner(goal)  # noqa: SLF001
    # Inject a HUMAN_REPLIED step manually (stand-in for coalesced reply).
    await repo.append_step(
        goal_id=goal.id, kind=GoalStepKind.HUMAN_REPLIED,
        text="ну норм", target_username="alice", target_user_id="uid-alice",
    )
    after_first = await repo.get(goal.id)
    assert after_first is not None
    await orch._invoke_planner(after_first)  # noqa: SLF001

    refreshed = await repo.get(goal.id)
    assert refreshed is not None
    assert refreshed.state == GoalState.AWAITING_REPLY  # not escalated
    # Both DMs sent to alice.
    alice_dms = [(uid, txt) for uid, txt in chat.sent_dms if uid == "uid-alice"]
    assert len(alice_dms) == 2


# ============================================================
#       5. REPLANNING crash recovery (sweep)
# ============================================================


@pytest.mark.asyncio
async def test_sweep_recovers_stuck_replanning(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Goal stuck in REPLANNING (last_planning_started_at older than
    config.replanning_stuck_after_minutes) is reverted to
    READY_TO_REPLAN by the sweep.
    """
    await _seed_task_row(session_factory)
    chat = _FakeChat()
    planner = _ScriptedPlanner([])  # not called
    orch = _make_orchestrator(session_factory, chat, planner)

    repo = GoalRepository(session_factory)
    goal = await _seed_goal(session_factory)
    await repo.update_state(goal.id, GoalState.COALESCING)
    await repo.claim_for_replan(goal.id)

    # Backdate started_at.
    async with session_scope(session_factory) as session:
        row = (await session.execute(
            select(GoalRow).where(GoalRow.id == goal.id)
        )).scalar_one()
        row.last_planning_started_at = (
            datetime.now(timezone.utc) - timedelta(minutes=30)
        )

    await orch.sweep_deadlines()

    refreshed = await repo.get(goal.id)
    assert refreshed is not None
    assert refreshed.state == GoalState.READY_TO_REPLAN


# ============================================================
#       6. wait_for_human + wake-up
# ============================================================


@pytest.mark.asyncio
async def test_wait_for_human_sets_next_planner_run_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``wait_for_human`` action puts the goal in WAITING and stamps
    ``next_planner_run_at``. Sweep does not re-fire it before then.
    """
    await _seed_task_row(session_factory)
    chat = _FakeChat()
    planner = _ScriptedPlanner([PlannerDecision(
        action=PlannerActionKind.WAIT_FOR_HUMAN,
        reasoning="ответит вечером", note="ответит вечером",
        retry_after_minutes=240,
    )])
    orch = _make_orchestrator(session_factory, chat, planner)

    repo = GoalRepository(session_factory)
    goal = await _seed_goal(session_factory)
    before = datetime.now(timezone.utc)
    await orch._invoke_planner(goal)  # noqa: SLF001

    refreshed = await repo.get(goal.id)
    assert refreshed is not None
    assert refreshed.state == GoalState.WAITING
    assert refreshed.next_planner_run_at is not None
    next_at = refreshed.next_planner_run_at
    if next_at.tzinfo is None:
        next_at = next_at.replace(tzinfo=timezone.utc)
    assert next_at >= before + timedelta(minutes=239)
    assert next_at <= before + timedelta(minutes=241)


@pytest.mark.asyncio
async def test_sweep_wakes_due_waiting_goal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When ``next_planner_run_at`` has elapsed, the sweep moves the
    goal back to READY_TO_REPLAN (and immediately re-runs the planner).
    """
    await _seed_task_row(session_factory)
    chat = _FakeChat(users={"alice": "uid-alice"})
    # When the wake-up runs the planner, scripted decision: ACHIEVE.
    planner = _ScriptedPlanner([PlannerDecision(
        action=PlannerActionKind.ACHIEVE,
        reasoning="finally got the answer",
        final_answer="ответ",
        confidence=0.9,
    )])
    orch = _make_orchestrator(session_factory, chat, planner)
    repo = GoalRepository(session_factory)
    goal = await _seed_goal(session_factory)

    # We need ANY unflushed fragment so _replan_after_reply doesn't bail
    # immediately. Add one before flipping state to WAITING.
    await repo.append_fragment(
        goal_id=goal.id, mm_post_id="p1", asked_post_id=None,
        text="late answer", received_at=datetime.now(timezone.utc),
    )

    # Set goal to WAITING with next_planner_run_at in the past.
    await repo.update_state(
        goal.id, GoalState.WAITING,
        next_planner_run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    await orch.sweep_deadlines()

    refreshed = await repo.get(goal.id)
    assert refreshed is not None
    # Either ACHIEVED (planner ran) or back to READY_TO_REPLAN (planner
    # didn't run yet — sweep's immediate replan is best-effort).
    assert refreshed.state in (GoalState.ACHIEVED, GoalState.READY_TO_REPLAN)
    # If the planner ran, it consumed the scripted ACHIEVE.
    if refreshed.state == GoalState.ACHIEVED:
        assert refreshed.final_answer == "ответ"


# ============================================================
#       Misc: send_pending retry, deadline abandon
# ============================================================


@pytest.mark.asyncio
async def test_send_pending_when_communicator_refuses(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When Communicator returns ``sent=False`` (rate-limit / outside
    working hours), the goal goes to SEND_PENDING with a retry counter
    instead of vanishing in PENDING.
    """
    await _seed_task_row(session_factory)

    class _RefusingChat(_FakeChat):
        async def send_direct(self, user_id: str, text: str) -> ChatMessage:  # type: ignore[override]
            raise RuntimeError("simulate transient send failure")

    chat = _RefusingChat(users={"alice": "uid-alice"})
    planner = _ScriptedPlanner([PlannerDecision(
        action=PlannerActionKind.ASK, reasoning="x",
        to_handle="alice", message="Q", dedupe_key="alice:q",
    )])
    orch = _make_orchestrator(session_factory, chat, planner)
    repo = GoalRepository(session_factory)
    goal = await _seed_goal(session_factory)
    await orch._invoke_planner(goal)  # noqa: SLF001

    refreshed = await repo.get(goal.id)
    assert refreshed is not None
    assert refreshed.state == GoalState.SEND_PENDING
    assert refreshed.send_retry_count == 1


@pytest.mark.asyncio
async def test_deadline_abandons_with_lead_dm(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_task_row(session_factory)
    chat = _FakeChat(users={"lead": "uid-lead"})
    planner = _ScriptedPlanner([])
    orch = _make_orchestrator(session_factory, chat, planner)

    repo = GoalRepository(session_factory)
    goal = await repo.create_goal(
        plan_id=1, tracker="jira", task_external_id="DM-1",
        description="x", why_it_matters="", initial_contact_hint="",
        coalesce_window_seconds=60,
        deadline_at=datetime.now(timezone.utc) - timedelta(seconds=30),
    )
    await orch.sweep_deadlines()

    refreshed = await repo.get(goal.id)
    assert refreshed is not None
    assert refreshed.state == GoalState.ABANDONED
    # Lead DM sent.
    lead_dms = [(uid, txt) for uid, txt in chat.sent_dms if uid == "uid-lead"]
    assert len(lead_dms) == 1


@pytest.mark.asyncio
async def test_circuit_breaker_after_max_planner_calls(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """After ``max_planner_calls_per_goal`` invocations without a
    terminal decision, the next call short-circuits to ESCALATED.
    """
    await _seed_task_row(session_factory)
    chat = _FakeChat(users={"lead": "uid-lead"})
    cfg = _config(max_planner_calls=2)
    planner = _ScriptedPlanner([])  # should NOT be called
    orch = _make_orchestrator(session_factory, chat, planner, config=cfg)

    repo = GoalRepository(session_factory)
    goal = await _seed_goal(session_factory)
    # Pre-load planner_calls_count to the circuit-breaker threshold.
    async with session_scope(session_factory) as session:
        row = (await session.execute(
            select(GoalRow).where(GoalRow.id == goal.id)
        )).scalar_one()
        row.planner_calls_count = 2
    refreshed = await repo.get(goal.id)
    assert refreshed is not None

    await orch._invoke_planner(refreshed)  # noqa: SLF001

    final = await repo.get(goal.id)
    assert final is not None
    assert final.state == GoalState.ESCALATED
    # No DMs sent except to lead.
    targets = {uid for uid, _ in chat.sent_dms}
    assert targets == {"uid-lead"}
