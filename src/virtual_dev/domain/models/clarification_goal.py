"""Domain models for the goal-driven clarification subsystem.

A ``ClarificationGoal`` is what the bot actually wants to know — for
example "получить пример request body для воспроизведения бага DM-3344".
The bot doesn't ask one fixed question and accept whatever comes back;
it iterates: a planner LLM looks at the goal + the full conversation
history + the latest reply and decides one next step (ask someone,
declare goal achieved, escalate, etc.). Each step is appended to
``GoalStep`` history so the planner has full context next time.

This replaces the Q-tree (``clarification.Question``): copying parent
question text on redirect was wrong — it loses the goal. With a goal
the planner re-composes the question for each new recipient.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class GoalState(str, Enum):
    """States a ``ClarificationGoal`` can be in.

    Active states: ``PENDING``, ``PLANNING``, ``SEND_PENDING``,
    ``AWAITING_REPLY``, ``COALESCING``, ``READY_TO_REPLAN``,
    ``REPLANNING``, ``WAITING``.
    Terminal states: ``ACHIEVED``, ``ABANDONED``, ``ESCALATED``.

    State graph (informal):

        PENDING → PLANNING → ASK→AWAITING_REPLY|SEND_PENDING / ACHIEVED / ESCALATED / ABANDONED / WAITING
        SEND_PENDING --(retry tick)--> AWAITING_REPLY | SEND_PENDING | ABANDONED(after N retries)
        AWAITING_REPLY --(fragment)--> COALESCING --(fragment)--> COALESCING (refresh idle)
        COALESCING --(idle ≥ window)--> READY_TO_REPLAN
        READY_TO_REPLAN --(coalescer tick)--> REPLANNING --(planner returns)--> ASK / ACHIEVE / ...
        REPLANNING --(timeout sweep)--> READY_TO_REPLAN  # crash-recovery
        WAITING --(next_planner_run_at passed)--> READY_TO_REPLAN
        any active --(deadline_at passed)--> ABANDONED
    """

    PENDING = "pending"
    PLANNING = "planning"
    SEND_PENDING = "send_pending"
    AWAITING_REPLY = "awaiting_reply"
    COALESCING = "coalescing"
    READY_TO_REPLAN = "ready_to_replan"
    REPLANNING = "replanning"
    WAITING = "waiting"
    ACHIEVED = "achieved"
    ABANDONED = "abandoned"
    ESCALATED = "escalated"


ACTIVE_STATES: frozenset[GoalState] = frozenset({
    GoalState.PENDING,
    GoalState.PLANNING,
    GoalState.SEND_PENDING,
    GoalState.AWAITING_REPLY,
    GoalState.COALESCING,
    GoalState.READY_TO_REPLAN,
    GoalState.REPLANNING,
    GoalState.WAITING,
})

TERMINAL_STATES: frozenset[GoalState] = frozenset({
    GoalState.ACHIEVED,
    GoalState.ABANDONED,
    GoalState.ESCALATED,
})


class GoalStepKind(str, Enum):
    """Kinds of audit-log entries appended to a goal's history."""

    BOT_ASKED = "bot_asked"               # the bot sent a DM
    HUMAN_REPLIED = "human_replied"       # coalesced reply received
    PLANNER_DECIDED = "planner_decided"   # planner emitted a decision
    NOTE = "note"                         # any other observation
    STALE_FRAGMENT = "stale_fragment"     # fragment that arrived but was superseded by a new ask


class PlannerActionKind(str, Enum):
    """The five actions a planner can decide on."""

    ASK = "ask"
    ACHIEVE = "achieve"
    ESCALATE_TO_LEAD = "escalate_to_lead"
    ABANDON = "abandon"
    WAIT_FOR_HUMAN = "wait_for_human"


@dataclass
class GoalStep:
    """One entry in the goal's append-only history.

    The planner sees the full chain of these on every invocation, so
    it can reason about what's been tried and what's still owed.
    """

    id: int
    goal_id: int
    seq: int
    kind: GoalStepKind
    timestamp: datetime
    text: str
    target_username: str | None = None
    target_user_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClarificationGoal:
    """An information need the bot wants to satisfy via humans.

    A goal owns at most one outstanding DM at a time (serial mode).
    When the human replies, the planner is invoked again and decides
    the next step. The goal stays alive until the planner declares it
    achieved/escalated/abandoned, or the deadline trips.
    """

    id: int
    plan_id: int | None
    tracker: str
    task_external_id: str

    description: str               # what the bot needs to learn
    why_it_matters: str            # passed verbatim to the planner
    initial_contact_hint: str      # raw ``ask_whom`` from the analyst (free text or "")

    state: GoalState
    final_answer: str | None = None  # populated when state == ACHIEVED

    # Currently-outstanding DM (set when state in
    # AWAITING_REPLY|COALESCING|READY_TO_REPLAN|REPLANNING).
    current_target_user_id: str | None = None
    current_target_username: str | None = None
    current_channel_id: str | None = None
    current_asked_post_id: str | None = None
    current_asked_text: str | None = None
    current_dedupe_key: str | None = None
    last_fragment_at: datetime | None = None

    # Idle window inherited from clarification.coalesce_window_seconds.
    coalesce_window_seconds: int = 600

    # Bookkeeping
    asked_at: datetime | None = None
    deadline_at: datetime | None = None
    closed_at: datetime | None = None
    next_planner_run_at: datetime | None = None  # set when state==WAITING
    planner_calls_count: int = 0
    send_retry_count: int = 0

    history: list[GoalStep] = field(default_factory=list)


@dataclass
class PlannerDecision:
    """One ``submit_decision`` payload from the planner."""

    action: PlannerActionKind
    reasoning: str = ""

    # Only for ASK
    to_handle: str | None = None
    to_email: str | None = None
    message: str | None = None
    dedupe_key: str | None = None

    # Only for ACHIEVE
    final_answer: str | None = None
    confidence: float = 0.0

    # Only for ESCALATE_TO_LEAD / ABANDON
    reason: str = ""

    # Only for WAIT_FOR_HUMAN
    note: str = ""
    retry_after_minutes: int | None = None

    # Bookkeeping (set by the agent runtime, not by the LLM)
    cost_usd: float = 0.0


__all__ = [
    "ACTIVE_STATES",
    "ClarificationGoal",
    "GoalState",
    "GoalStep",
    "GoalStepKind",
    "PlannerActionKind",
    "PlannerDecision",
    "TERMINAL_STATES",
]
