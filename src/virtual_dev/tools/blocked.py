"""Terminal — ticket is blocked / unworkable.

Triggers a Jira transition to "Waiting For Response", a comment
explaining why, and a DM to the team-lead.
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
        "blocked",
        "Mark the ticket BLOCKED / unworkable. The bot will: "
        "(1) transition Jira to \"Waiting For Response\", "
        "(2) post an explanatory comment on the ticket, "
        "(3) DM the team-lead with the conversation chain. "
        "Use when the ticket self-contradicts, depends on missing "
        "external info that nobody can provide right now, or has been "
        "cancelled. NOT for \"I'm just stuck\" — call `stuck` for that.",
        {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    )
    async def _blocked(args: dict[str, Any]) -> dict[str, Any]:
        if run_state.get("ask_dispatched"):
            return wrap_text({
                "recorded": False, "reason": "ask_pending",
                "instruction": "ASK in flight — end your turn first.",
            })
        if run_state.get("terminal"):
            return wrap_text({"recorded": False, "reason": "already_terminal"})
        reason = str(args.get("reason") or "").strip() or "no_reason"
        effects.append(AnalystEffect(
            kind="blocked", payload={"reason": reason},
        ))
        run_state["terminal"] = True
        return wrap_text({"recorded": True, "instruction": "Blocked. End your turn."})

    return _blocked
