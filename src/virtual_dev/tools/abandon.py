"""Terminal — soft give-up. Close without escalating."""

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
        "abandon",
        "Soft give-up — close without escalating. Use when the "
        "ticket self-contradicts or is no longer relevant.",
        {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    )
    async def _abandon(args: dict[str, Any]) -> dict[str, Any]:
        if run_state.get("ask_dispatched"):
            return wrap_text({
                "recorded": False, "reason": "ask_pending",
                "instruction": "ASK in flight — end your turn first.",
            })
        if run_state.get("terminal"):
            return wrap_text({"recorded": False, "reason": "already_terminal"})
        reason = str(args.get("reason") or "").strip() or "no_reason"
        effects.append(AnalystEffect(
            kind="abandon", payload={"reason": reason},
        ))
        run_state["terminal"] = True
        return wrap_text({"recorded": True, "instruction": "Abandoned. End your turn."})

    return _abandon
