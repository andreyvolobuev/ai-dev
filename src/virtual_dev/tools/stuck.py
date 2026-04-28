"""Terminal — analyst is stuck; DM the team-lead with the chain.

Use this when you've tried multiple angles and can't make progress
but the ticket isn't blocked per se — you just need a human to look.
The Jira ticket stays in "In Progress"; only the lead is paged.

For tickets that are *actually* blocked (missing API spec, contradictory
requirements, cancelled work) call `blocked` instead — that one moves
the Jira ticket to "Waiting For Response" and comments why.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from virtual_dev.application.services.agent_effects import AnalystEffect
from virtual_dev.tools import ToolContext, wrap_text

TOOL_GROUP = "analyst"


def build(ctx: ToolContext):
    if ctx.effects is None or ctx.run_state is None:
        return None
    effects = ctx.effects
    run_state = ctx.run_state

    @tool(
        "stuck",
        "Give up and DM the team-lead with the conversation chain. "
        "Use when you're truly stuck after multiple angles but the "
        "ticket itself isn't blocked — you just need a human to look. "
        "Ticket stays In Progress. For ACTUALLY blocked tickets "
        "(missing spec, contradictions) call `blocked` instead.",
        {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    )
    async def _stuck(args: dict[str, Any]) -> dict[str, Any]:
        if run_state.get("ask_dispatched"):
            return wrap_text({
                "recorded": False, "reason": "ask_pending",
                "instruction": "ASK in flight — end your turn first.",
            })
        if run_state.get("terminal"):
            return wrap_text({"recorded": False, "reason": "already_terminal"})
        reason = str(args.get("reason") or "").strip() or "no_reason"
        effects.append(AnalystEffect(
            kind="stuck", payload={"reason": reason},
        ))
        run_state["terminal"] = True
        return wrap_text({"recorded": True, "instruction": "Lead will be DM'd. End your turn."})

    return _stuck
