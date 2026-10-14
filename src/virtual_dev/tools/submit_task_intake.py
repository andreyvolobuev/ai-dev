"""Terminal — TaskIntake submits what to do with a Mattermost ask.

The intake agent reads a post that mentions the bot (plus the thread it
sits in, if any) and decides: create a Jira task, patch the task it
already created for this thread, or politely decline because the ask
isn't about a task at all.

The tool only *captures* the decision — every side effect (Jira write,
sprint, assignee, the reply in the thread) is done by
``runtime/workers/intake_inbox.py``. That split is deliberate: the model
reads untrusted chat text, so it must not hold a pen over Jira.

Lives in the ``intake`` group, so analyst / dev / responder don't see it.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from virtual_dev.tools import ToolContext, wrap_text

TOOL_GROUP = "intake"

_SUBMIT_INTAKE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["create", "update", "busy"]},
        "summary": {"type": "string"},
        "description": {"type": "string"},
        "assignee_hint": {"type": "string"},
        "changes": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "description": {"type": "string"},
                "assignee_hint": {"type": "string"},
                "sprint": {"type": "boolean"},
            },
        },
        "reply_text": {"type": "string"},
        "reasoning": {"type": "string"},
    },
    "required": ["action", "reasoning"],
}


def build(ctx: ToolContext) -> SdkMcpTool[Any] | None:
    if ctx.submit_capture is None or ctx.run_state is None:
        return None
    submit_capture = ctx.submit_capture
    run_state = ctx.run_state

    @tool(
        "submit_task_intake",
        "Submit what to do with this Mattermost ask. Call exactly once "
        "at the end. 'create' — the person wants a new Jira task "
        "(give summary + description, and assignee_hint only if they "
        "named someone else); 'update' — they want the task you already "
        "created in this thread changed (put the deltas in changes); "
        "'busy' — the ask is not about a task, decline briefly in "
        "reply_text.",
        _SUBMIT_INTAKE_SCHEMA,
    )
    async def _submit(args: dict[str, Any]) -> dict[str, Any]:
        if run_state.get("terminal"):
            return wrap_text({"recorded": False, "reason": "already_terminal"})
        action = str(args.get("action") or "").lower()
        # A create without a summary would produce a nameless ticket
        # nobody can find later — reject and let the model re-call.
        if action == "create" and not str(args.get("summary") or "").strip():
            return wrap_text({
                "recorded": False,
                "reason": "missing_summary",
                "instruction": (
                    "action='create' requires a non-empty summary — it "
                    "becomes the Jira ticket title. Call "
                    "submit_task_intake again with summary filled in."
                ),
            })
        if action == "update" and not (args.get("changes") or {}):
            return wrap_text({
                "recorded": False,
                "reason": "missing_changes",
                "instruction": (
                    "action='update' requires at least one field in "
                    "changes. If nothing should change, use action='busy'."
                ),
            })
        if action == "busy" and not str(args.get("reply_text") or "").strip():
            return wrap_text({
                "recorded": False,
                "reason": "missing_reply_text",
                "instruction": (
                    "action='busy' requires reply_text — it is the only "
                    "thing the human sees. One or two sentences."
                ),
            })
        submit_capture.clear()
        submit_capture.update(args)
        run_state["terminal"] = True
        return wrap_text({"recorded": True})

    return _submit


__all__ = ["TOOL_GROUP", "_SUBMIT_INTAKE_SCHEMA", "build"]
