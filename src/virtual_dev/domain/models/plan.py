"""Domain model for an implementation plan produced by the Analyst agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class PlanStatus(str, Enum):
    DRAFT = "draft"             # still being worked on
    READY = "ready"              # clean plan, no open questions
    CLARIFYING = "clarifying"    # has open questions for humans
    SUPERSEDED = "superseded"    # replaced by a newer plan for the same task
    FAILED = "failed"


@dataclass
class PlanStep:
    """A single actionable step in the plan."""

    order: int
    summary: str                 # short title of the step
    details: str = ""            # longer description
    repo_key: str | None = None  # repo this step touches, if known
    files_touched: list[str] = field(default_factory=list)


@dataclass
class OpenQuestion:
    """Something the Analyst could not decide on its own."""

    question: str
    why_it_matters: str = ""     # impact on the plan
    ask_whom: str | None = None  # hint: "author of <file>", "channel <X>", ...


@dataclass
class Plan:
    """Output of the Analyst agent for a single task.

    Persisted so the dashboard can show it and later agents (Dev, Reviewer)
    can consume it.
    """

    task_external_id: str
    tracker: str

    summary: str                         # one-paragraph gist of the plan
    steps: list[PlanStep] = field(default_factory=list)
    open_questions: list[OpenQuestion] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    confidence: float = 0.5              # 0..1; self-assessment by Analyst

    status: PlanStatus = PlanStatus.DRAFT
    target_repo_key: str | None = None   # determined by Analyst from components / hints

    # Bookkeeping — informational only. We run on Claude Max (no metered
    # billing), so cost_usd is the SDK's estimate shown in the dashboard,
    # not a figure we enforce against.
    cost_usd: float = 0.0
    iterations: int = 0
    model: str = ""
    agent_key: str = ""                  # which agent produced this
    created_at: datetime | None = None
