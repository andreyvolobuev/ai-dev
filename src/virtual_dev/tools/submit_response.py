"""Terminal — ThreadResponder submits its decision for a review reply.

The ThreadResponder reads a Mattermost review-thread reply and decides
whether to ``reply`` in the thread, ``iterate`` (kick off a Dev-agent
re-run with new feedback), or ``ignore`` (chatter, no action). This
tool captures the LLM's structured decision so the surrounding
runtime can act on it.

Lives in the ``responder`` group, so analyst / dev don't see it.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from virtual_dev.tools import ToolContext, wrap_text

TOOL_GROUP = "responder"

_SUBMIT_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["reply", "iterate", "ignore"]},
        "reply_text": {"type": "string"},
        "iteration_feedback": {"type": "string"},
        "reasoning": {"type": "string"},
    },
    "required": ["action", "reasoning"],
}


def build(ctx: ToolContext):
    if ctx.submit_capture is None:
        return None
    submit_capture = ctx.submit_capture

    @tool(
        "submit_response",
        "Submit your decision on this review-thread reply. Call "
        "exactly once at the end. Action: 'reply' to post text back, "
        "'iterate' to ask Dev to update the MR (also include the "
        "feedback to act on), 'ignore' for chatter that doesn't need "
        "a response.",
        _SUBMIT_RESPONSE_SCHEMA,
    )
    async def _submit(args: dict[str, Any]) -> dict[str, Any]:
        submit_capture.clear()
        submit_capture.update(args)
        return wrap_text({"recorded": True, "instruction": "Decision recorded."})

    return _submit


__all__ = ["TOOL_GROUP", "_SUBMIT_RESPONSE_SCHEMA", "build"]
