"""META tool: soft give-up — close the task without DMing the lead."""

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
        "reason": {"type": "string"},
    },
    "required": ["reason"],
}


@tool_(
    name="abandon",
    description=(
        "Soft give-up: close the task without escalating. Use when "
        "the task turns out to be unnecessary (the issue self-"
        "contradicts, became obsolete, or you've concluded a human "
        "follow-up is genuinely not needed). Different from "
        "escalate_to_lead — no lead-DM is sent."
    ),
    schema=_SCHEMA,
    mode=ToolMode.META,
    tags={"clarification"},
)
async def abandon(
    args: dict[str, Any], ctx: ToolContext,
) -> ToolOutcome:
    reason = str(args.get("reason") or "").strip() or "no_reason_given"
    return ToolOutcome(
        mode=ToolMode.META,
        meta_action="abandon",
        meta_payload={"reason": reason},
    )
