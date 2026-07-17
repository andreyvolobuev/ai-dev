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
        "action": {
            "type": "string",
            "enum": [
                "reply", "iterate", "ignore", "propose_alternative",
            ],
        },
        "reply_text": {"type": "string"},
        "iteration_feedback": {"type": "string"},
        "reasoning": {"type": "string"},
    },
    "required": ["action", "reasoning"],
}


def build(ctx: ToolContext):
    if ctx.submit_capture is None or ctx.run_state is None:
        return None
    submit_capture = ctx.submit_capture
    run_state = ctx.run_state

    @tool(
        "submit_response",
        "Submit your decision on this review-thread reply. Call "
        "exactly once at the end. Action: 'reply' to post text back, "
        "'iterate' to ask Dev to update the MR (also include the "
        "feedback to act on), 'ignore' for chatter that doesn't need "
        "a response, 'propose_alternative' to push back with a better "
        "approach when the reviewer's request would degrade the system "
        "(N+1, broken invariants, perf regression) — same payload as "
        "'reply' (use reply_text for the explanation + alternative).",
        _SUBMIT_RESPONSE_SCHEMA,
    )
    async def _submit(args: dict[str, Any]) -> dict[str, Any]:
        if run_state.get("terminal"):
            return wrap_text({"recorded": False, "reason": "already_terminal"})
        # A reply-класс decision without the actual text is a known model
        # glitch (the reply ends up pasted inside `reasoning` as fake XML).
        # Recording it would silently drop the human's comment — reject and
        # let the model re-call with the text in the right field.
        action = str(args.get("action") or "").lower()
        if action in ("reply", "propose_alternative") and not str(
            args.get("reply_text") or ""
        ).strip():
            return wrap_text({
                "recorded": False,
                "reason": "missing_reply_text",
                "instruction": (
                    f"action={action!r} requires a non-empty reply_text — "
                    "that field is the ONLY thing the human sees. Call "
                    "submit_response again with the reply in reply_text "
                    "(not inside reasoning)."
                ),
            })
        submit_capture.clear()
        submit_capture.update(args)
        run_state["terminal"] = True
        return wrap_text({"recorded": True, "instruction": "Decision recorded."})

    return _submit


__all__ = ["TOOL_GROUP", "_SUBMIT_RESPONSE_SCHEMA", "build"]
