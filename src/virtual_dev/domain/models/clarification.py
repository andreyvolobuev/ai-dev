"""Domain models for the clarification subsystem.

ISSUE → produces a Plan; if information is missing, Analyst emits open
QUESTIONs. Each Question has a STAKEHOLDER (responsible human) and
REASONING (why_it_matters). When a stakeholder replies, we coalesce
multi-message answers, run them through an LLM classifier, and either
record a DIRECT answer, spawn a child Question (REDIRECT /
COUNTER_QUESTION / ASKING_FOR_STAKEHOLDER), or terminate
(DONT_KNOW / OUT_OF_SCOPE).

The *application source of truth* is :class:`Question` (a dataclass
graph). The DB row is just its projection — same idiom as
``Plan``/``PlanRow``. All field names mirror :class:`QuestionRow`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class QuestionState(str, Enum):
    """States a Question can be in.

    Active states: ``PENDING``, ``ASKING``, ``COALESCING``,
    ``CLASSIFYING``, ``COUNTER_PENDING``, ``ASKING_FOR_STAKEHOLDER``.
    Terminal states: ``ANSWERED``, ``REDIRECTED``, ``ABANDONED``,
    ``ESCALATED``.
    """

    PENDING = "pending"                                # row created, DM not yet sent
    ASKING = "asking"                                  # DM sent, waiting for the human
    COALESCING = "coalescing"                          # fragments arriving; idle timer running
    CLASSIFYING = "classifying"                        # coalesced text in flight to LLM (soft-lock)
    ANSWERED = "answered"                              # DIRECT answer captured; closed
    REDIRECTED = "redirected"                          # spawned a child to a new stakeholder; closed
    COUNTER_PENDING = "counter_pending"                # we owe the asker a reply; child question in flight
    ASKING_FOR_STAKEHOLDER = "asking_for_stakeholder"  # asking original respondent for a missing handle
    ABANDONED = "abandoned"                            # OUT_OF_SCOPE / loop guard / cycle / timeout
    ESCALATED = "escalated"                            # team-lead notified


class StakeholderKind(str, Enum):
    """How a stakeholder was identified.

    Order roughly tracks resolution precedence in
    :class:`StakeholderResolver`. ``UNRESOLVED_NAME`` is the only
    "needs more work" kind — orchestrator treats it specially by
    spawning an ``ASKING_FOR_STAKEHOLDER`` child.
    """

    EXPLICIT_HANDLE = "explicit_handle"   # @vasya / vasya.kurochkin → resolved to MM user
    EMAIL = "email"                       # vasya@2gis.ru
    TASK_AUTHOR = "task_author"           # task_row.reporter_id
    TEAM_CHANNEL = "team_channel"         # mappings.team_channels[<repo>]
    TEAM_LEAD = "team_lead"               # escalation.mattermost_user
    UNRESOLVED_NAME = "unresolved_name"   # raw "Вася Курочкин" — needs LLM/MM search
    BOT = "bot"                           # us — for COUNTER_QUESTION sub-questions where bot self-answers


class Classification(str, Enum):
    """LLM classification of a coalesced answer."""

    DIRECT = "direct"
    REDIRECT = "redirect"
    COUNTER_QUESTION = "counter_question"
    DONT_KNOW = "dont_know"
    OUT_OF_SCOPE = "out_of_scope"
    HANDLE_PROVIDED = "handle_provided"   # only valid for ASKING_FOR_STAKEHOLDER children


class CounterQuestionKind(str, Enum):
    """Sub-classification for ``COUNTER_QUESTION``: who answers it."""

    FACTUAL = "factual"     # answerable from issue + repo (bot self-answers)
    BUSINESS = "business"   # priority/intent — escalate to task author


class OutOfScopeKind(str, Enum):
    """Sub-classification for ``OUT_OF_SCOPE``."""

    ABUSE = "abuse"
    WRONG_PERSON = "wrong_person"
    LEAVE_ME_ALONE = "leave_me_alone"


# --- Value objects ---


@dataclass
class Stakeholder:
    """The human (or channel, or the bot itself) we ask one Question.

    ``raw_hint`` stores exactly what the analyst (or a redirect answer)
    wrote — useful when the resolved-handle later turns out wrong and
    we have to debug.
    """

    kind: StakeholderKind
    raw_hint: str
    resolved_mm_user_id: str | None = None
    resolved_mm_channel_id: str | None = None
    display_name: str | None = None


@dataclass
class AnswerFragment:
    """One raw MM message, before coalescing."""

    mm_post_id: str
    text: str
    received_at: datetime


@dataclass
class Answer:
    """Coalesced + classified result of one or more fragments."""

    fragments: list[AnswerFragment] = field(default_factory=list)
    coalesced_text: str = ""
    classification: Classification | None = None
    extracted: dict[str, Any] = field(default_factory=dict)
    classified_at: datetime | None = None
    cost_usd: float = 0.0


# --- Aggregate ---


@dataclass
class Question:
    """One node in the Q-tree for an Issue.

    A root question has ``parent_id is None`` and ``id == root_id``.
    Children inherit ``root_id`` and ``plan_id`` from their root, and
    ``chain_depth = parent.chain_depth + 1``.
    """

    id: int
    root_id: int
    parent_id: int | None
    chain_depth: int
    state: QuestionState

    text: str
    why_it_matters: str

    stakeholder: Stakeholder

    asked_post_id: str | None = None
    mm_user_id: str | None = None
    mm_channel_id: str | None = None
    last_fragment_at: datetime | None = None
    deadline_at: datetime | None = None

    answer: Answer | None = None

    tracker: str = ""
    task_external_id: str = ""
    plan_id: int | None = None      # only the root carries this

    asked_at: datetime | None = None
    closed_at: datetime | None = None
    coalesce_window_seconds: int = 600


# Active vs terminal — convenience sets used by the orchestrator's
# settled-check and by the deadline sweeper.
ACTIVE_STATES: frozenset[QuestionState] = frozenset({
    QuestionState.PENDING,
    QuestionState.ASKING,
    QuestionState.COALESCING,
    QuestionState.CLASSIFYING,
    QuestionState.COUNTER_PENDING,
    QuestionState.ASKING_FOR_STAKEHOLDER,
})

TERMINAL_STATES: frozenset[QuestionState] = frozenset({
    QuestionState.ANSWERED,
    QuestionState.REDIRECTED,
    QuestionState.ABANDONED,
    QuestionState.ESCALATED,
})


__all__ = [
    "ACTIVE_STATES",
    "Answer",
    "AnswerFragment",
    "Classification",
    "CounterQuestionKind",
    "OutOfScopeKind",
    "Question",
    "QuestionState",
    "Stakeholder",
    "StakeholderKind",
    "TERMINAL_STATES",
]
