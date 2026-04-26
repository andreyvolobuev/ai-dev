"""META tool: give up on the task and DM the team-lead with the chain."""

from __future__ import annotations

from typing import Any

from virtual_dev.application.services.clarification.tools import (
    ToolContext,
    ToolMode,
    ToolOutcome,
    tool_,
)


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {
            "type": "string",
            "description": (
                "Short, in the issue's language. Goes verbatim into "
                "the lead's DM template."
            ),
        },
    },
    "required": ["reason"],
}


@tool_(
    name="escalate_to_lead",
    description=(
        "Goal is stuck and a human needs to step in. The orchestrator "
        "DMs the configured team-lead with the full chain of steps. "
        "Use when self-research is exhausted, respondent doesn't know, "
        "or the issue genuinely needs a human's intent decision."
    ),
    schema=_SCHEMA,
    mode=ToolMode.META,
    tags={"clarification"},
)
async def escalate_to_lead(
    args: dict[str, Any], ctx: ToolContext,
) -> ToolOutcome:
    reason = str(args.get("reason") or "").strip() or "no_reason_given"
    return ToolOutcome(
        mode=ToolMode.META,
        meta_action="escalate_to_lead",
        meta_payload={"reason": reason},
    )
