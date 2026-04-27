"""Domain models for the analyst's per-ticket conversation log (Phase 5.0).

The Analyst is now a continuous-reasoning agent: it researches, DMs
humans when stuck, reads their replies, and eventually submits a
plan. Across human-reply latency the SDK session is one-shot, so we
persist the conversation history per ticket and re-render it into
the analyst's user prompt on every invocation.

This module defines the in-memory shape; the corresponding tables
are :class:`virtual_dev.infrastructure.db.AnalystConversationStepRow`
and :class:`AnalystConversationFragmentRow`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class ConversationStepKind(str, Enum):
    """Kinds of audit-log entries appended to a task's conversation log."""

    PLANNER_DECIDED = "planner_decided"   # one analyst run summary
    BOT_ASKED = "bot_asked"               # bot DM'd a human
    HUMAN_REPLIED = "human_replied"       # coalesced reply received
    NOTE = "note"                         # generic observation
    STALE_FRAGMENT = "stale_fragment"     # archived buffered fragment


@dataclass
class ConversationStep:
    """One entry in a task's append-only conversation log."""

    id: int
    task_id: int          # TaskRow.id (the tracker ticket)
    seq: int
    kind: ConversationStepKind
    timestamp: datetime
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


__all__ = ["ConversationStep", "ConversationStepKind"]
