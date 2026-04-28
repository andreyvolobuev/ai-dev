"""Terminal — submit the analyst's READY plan to the orchestrator."""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from virtual_dev.application.services.agent_effects import AnalystEffect
from virtual_dev.tools import ToolContext, wrap_text

TOOL_GROUP = "analyst"

_SUBMIT_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "order": {"type": "integer"},
                    "summary": {"type": "string"},
                    "details": {"type": "string"},
                    "repo_key": {"type": ["string", "null"]},
                    "files_touched": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["order", "summary"],
            },
        },
        # Vestigial — phase 5.0 has no separate clarifying flow.
        "open_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "why_it_matters": {"type": "string"},
                    "ask_whom": {"type": ["string", "null"]},
                },
                "required": ["question"],
            },
        },
        "risks": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
        "target_repo_key": {"type": ["string", "null"]},
        "status": {"type": "string", "enum": ["ready", "failed"]},
    },
    "required": ["summary", "steps", "risks", "confidence", "status"],
}


def build(ctx: ToolContext):
    if ctx.effects is None or ctx.submit_capture is None or ctx.run_state is None:
        return None
    effects = ctx.effects
    submit_capture = ctx.submit_capture
    run_state = ctx.run_state

    @tool(
        "submit_plan",
        "Submit your final, READY plan. Call this when you've "
        "gathered all info needed and a Dev agent could implement "
        "from the steps. Status MUST be 'ready'. If something's "
        "still missing, use dm_user instead.",
        _SUBMIT_PLAN_SCHEMA,
    )
    async def _submit(args: dict[str, Any]) -> dict[str, Any]:
        if run_state.get("ask_dispatched"):
            return wrap_text({
                "recorded": False,
                "reason": "ask_pending",
                "instruction": (
                    "You called dm_user this turn — that DM is "
                    "in flight, you don't have the answer yet. "
                    "END YOUR TURN now. The orchestrator will "
                    "re-invoke you with the human's reply, and "
                    "only THEN you can submit_plan once you've "
                    "actually got what you needed."
                ),
            })
        if run_state.get("terminal"):
            return wrap_text({"recorded": False, "reason": "already_terminal"})
        submit_capture.clear()
        submit_capture.update(args)
        effects.append(AnalystEffect(
            kind="plan_submitted",
            payload={
                "summary": str(args.get("summary") or "")[:200],
                "status": str(args.get("status") or "ready"),
                "target_repo_key": args.get("target_repo_key"),
            },
        ))
        run_state["terminal"] = True
        return wrap_text({"recorded": True, "instruction": "Plan recorded. End your turn."})

    return _submit


__all__ = ["TOOL_GROUP", "_SUBMIT_PLAN_SCHEMA", "build"]
