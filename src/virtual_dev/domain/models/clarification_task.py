"""Domain models for the task-driven clarification subsystem (Phase 4.5).

Replaces the goal-driven model. The shape is the user's spec:

    class ClarificationTask:
        id, parent_id, question, info_source, info_source_class,
        current_response, is_solved

A task is a single information need. The agent loop, per task:

    1. Look at the available tools (registry).
    2. The Planner LLM picks ONE next tool to apply (or `decompose` /
       `escalate_to_lead` / `abandon`).
    3. The orchestrator executes the tool. SYNC tools return data
       immediately (e.g. `find_mm_user_by_name`); ASYNC tools start a
       conversation and the next iteration runs after the reply
       coalesces; META tools mutate the tree (decompose creates
       subtasks; escalate / abandon close the task).
    4. The Validator LLM receives the response + the WHOLE chain of
       ancestors and decides which task(s) the response actually
       resolves. Chain validation: the user replying to a sub-task
       may incidentally answer the root — we recognise that and skip
       the rest.
    5. If a task isn't solved, the planner picks the next tool. The
       loop runs until the task is solved or guards trip.

Every interaction is recorded as a ``TaskStep`` so the planner has
full context on every turn and humans can audit decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class TaskStepKind(str, Enum):
    """Kinds of audit-log entries appended to a task's history."""

    PLANNER_DECIDED = "planner_decided"   # planner picked a tool
    TOOL_INVOKED = "tool_invoked"         # orchestrator ran the tool
    TOOL_RESULT = "tool_result"           # tool returned a payload
    BOT_ASKED = "bot_asked"               # bot DM'd a human (ASYNC tool)
    HUMAN_REPLIED = "human_replied"       # coalesced reply received
    VALIDATED = "validated"               # validator emitted a verdict
    STALE_FRAGMENT = "stale_fragment"     # late fragment, archived
    SUBTASK_SPAWNED = "subtask_spawned"   # decomposition created a child
    SUBTASK_RESOLVED = "subtask_resolved" # child finished
    NOTE = "note"                         # any other observation


class ToolMode(str, Enum):
    """How the orchestrator runs a tool.

    SYNC — the tool returns a payload immediately; the validator runs
    on it inline and the planner re-decides on the next tick.

    ASYNC — the tool starts a human-facing conversation (DM, channel
    post). Control returns to the loop; when a human reply coalesces,
    the validator runs on the merged reply.

    META — the tool mutates the task tree directly (decompose creates
    subtasks; escalate_to_lead / abandon close the task). No
    validator run; orchestrator handles state transitions.
    """

    SYNC = "sync"
    ASYNC = "async"
    META = "meta"


@dataclass
class ToolInvocation:
    """One call the planner asked for: tool name + JSON-shaped params."""

    tool: str
    params: dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""


@dataclass
class ToolResult:
    """What the orchestrator extracted from running a SYNC tool.

    For ASYNC tools, ``ToolResult`` is built when the human reply
    arrives (text = the coalesced reply, source_label = MM handle).
    For META tools no ToolResult is produced.
    """

    text: str = ""                       # canonical text payload
    structured: dict[str, Any] | None = None
    source_label: str = ""               # who/what produced this (handle / file / url)
    source_class: str = ""               # mattermost | confluence | file | …
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskStep:
    """One entry in a task's append-only history."""

    id: int
    task_id: int
    seq: int
    kind: TaskStepKind
    timestamp: datetime
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClarificationTask:
    """An information need the bot is trying to satisfy.

    Tasks form a tree via ``parent_id``. A task is "active" until
    ``is_solved`` flips True or it is abandoned/escalated. The agent
    runs a tool-pick / apply / validate loop on each tick; an ASYNC
    tool installs ``awaiting_*`` fields and pauses until the reply
    coalesces.
    """

    id: int
    plan_id: int | None
    parent_id: int | None
    tracker: str
    task_external_id: str

    question: str
    info_source: str | None = None       # filled when the planner identifies who/what should answer
    info_source_class: str | None = None # mattermost | confluence | file | …
    current_response: str | None = None  # last raw answer received (validated or not)
    is_solved: bool = False
    final_answer: str | None = None      # validator's synthesized answer
    confidence: float = 0.0

    depth: int = 0

    # Loop bookkeeping.
    iteration_count: int = 0
    tools_tried: list[str] = field(default_factory=list)
    closed: bool = False                 # solved OR abandoned/escalated terminal

    # Outstanding async wait (filled when an ASYNC tool starts a
    # conversation; cleared when the reply coalesces).
    awaiting_post_id: str | None = None
    awaiting_user_id: str | None = None
    awaiting_username: str | None = None
    awaiting_channel_id: str | None = None
    awaiting_dedupe_key: str | None = None
    last_fragment_at: datetime | None = None
    coalesce_window_seconds: int = 60

    # Lifecycle stamps.
    created_at: datetime | None = None
    deadline_at: datetime | None = None
    solved_at: datetime | None = None
    closed_at: datetime | None = None
    last_planning_started_at: datetime | None = None
    next_planner_run_at: datetime | None = None  # set when planner returned `wait`

    history: list[TaskStep] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return not self.closed


__all__ = [
    "ClarificationTask",
    "TaskStep",
    "TaskStepKind",
    "ToolInvocation",
    "ToolMode",
    "ToolResult",
]
