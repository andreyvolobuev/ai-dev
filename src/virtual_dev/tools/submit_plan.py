"""Terminal — submit the analyst's READY plan to the orchestrator."""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from virtual_dev.application.services.agent_effects import AnalystEffect
from virtual_dev.tools import ToolContext, wrap_text
from virtual_dev.tools._helpers import scrub_human_fields, strip_agent_attribution
from virtual_dev.tools._team_thread import (
    collect_late_replies,
    late_replies_result,
)

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
        # Everything the plan assumes that the ticket / sources don't
        # state outright. Non-empty → the plan needs a human sign-off.
        "decisions": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
        # One short line for the team thread: «беру в работу». Written by
        # the model so it doesn't read as the same canned bot phrase.
        "thread_note": {"type": ["string", "null"]},
        "target_repo_key": {"type": ["string", "null"]},
        "status": {"type": "string", "enum": ["ready", "failed"]},
    },
    "required": ["summary", "steps", "risks", "decisions", "confidence", "status"],
}


def build(ctx: ToolContext):
    if ctx.effects is None or ctx.submit_capture is None or ctx.run_state is None:
        return None
    effects = ctx.effects
    submit_capture = ctx.submit_capture
    run_state = ctx.run_state
    communicator = ctx.communicator

    @tool(
        "submit_plan",
        "Submit your final, READY plan. Call this when you've "
        "gathered all info needed and a Dev agent could implement "
        "from the steps. Status MUST be 'ready'. If something's "
        "still missing, use dm_user / ask_team instead. `decisions` "
        "lists every choice the ticket and its sources don't state "
        "outright: picked interpretations, options marked as "
        "proposed / not agreed, scope beyond the ticket, schema "
        "migrations or data changes. A plan with decisions (or low "
        "confidence) is refused until the team has replied to your "
        "proposal or let it stand (silence for a couple of working "
        "hours is consent). `thread_note`: if you discussed this in the "
        "team thread, one short line posted there that you're starting "
        "(«Возражений нет — беру в работу»; vary the wording).",
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
        if communicator is not None:
            late = await collect_late_replies(communicator, run_state, effects)
            if late is not None:
                root_id = run_state["team_thread"]["root_id"]
                return wrap_text({
                    "recorded": False, **late_replies_result(late, root_id=root_id),
                })
        if _needs_confirmation(args, run_state):
            return wrap_text({
                "recorded": False,
                "reason": "needs_confirmation",
                "instruction": (
                    "This plan rests on decisions the team hasn't seen "
                    "(or your confidence is low). Tell the team what you're "
                    "about to do with ask_team — one short line the way a "
                    "colleague would («@vasya, по DM-1 перепишу ручку /data "
                    "на asyncio»), @-mentioning whose area it touches; no "
                    "list of changes, no «скажите ок» — and END YOUR TURN. "
                    "Objections come back as replies; silence for a couple "
                    "of working hours is consent and you'll be re-invoked "
                    "to submit. No team channel → DM the reporter instead."
                ),
            })
        submit_capture.clear()
        # summary, risks and decisions are rendered into the Jira comment.
        submit_capture.update(
            scrub_human_fields(args, ("summary", "risks", "decisions")),
        )
        effects.append(AnalystEffect(
            kind="plan_submitted",
            payload={
                "summary": str(args.get("summary") or "")[:200],
                "status": str(args.get("status") or "ready"),
                "target_repo_key": args.get("target_repo_key"),
                "thread_note": strip_agent_attribution(str(args.get("thread_note") or "")),
            },
        ))
        run_state["terminal"] = True
        return wrap_text({"recorded": True, "instruction": "Plan recorded. End your turn."})

    return _submit


def _needs_confirmation(args: dict[str, Any], run_state: dict[str, Any]) -> bool:
    if run_state.get("proposal_confirmed"):
        return False
    if str(args.get("status") or "").lower() == "failed":
        return False
    if any(str(d).strip() for d in args.get("decisions") or []):
        return True
    threshold = float(run_state.get("confirm_below_confidence") or 0.0)
    try:
        confidence = float(args.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return confidence < threshold


__all__ = ["TOOL_GROUP", "_SUBMIT_PLAN_SCHEMA", "build"]
