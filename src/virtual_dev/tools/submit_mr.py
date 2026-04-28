"""Terminal — Dev agent submits its MR title / description / status.

The Dev agent edits / commits / pushes the workspace itself; this tool
just captures the LLM's final structured submission so the surrounding
runtime can use it for the MR body, the commit message, and the
``DevResult.submission`` field downstream consumers read. Calling it
is the agent's signal that its work is done.

Lives in the ``dev`` group, so the analyst (which only registers
``analyst`` + ``researcher``) doesn't see it in its tool surface.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from virtual_dev.tools import ToolContext, wrap_text

TOOL_GROUP = "dev"

_SUBMIT_MR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "status": {"type": "string", "enum": ["success", "failed"]},
        "notes": {"type": "string"},
    },
    "required": ["title", "description", "status"],
}


def build(ctx: ToolContext):
    if ctx.submit_capture is None:
        return None
    submit_capture = ctx.submit_capture

    @tool(
        "submit_mr",
        "Call this exactly once at the end. Provide the MR title and "
        "a detailed description of what was done. Status is 'success' "
        "if the change is ready for review, 'failed' if you couldn't "
        "complete the task. The runtime commits + pushes + opens the "
        "draft MR for you — don't shell out to git yourself.",
        _SUBMIT_MR_SCHEMA,
    )
    async def _submit_mr(args: dict[str, Any]) -> dict[str, Any]:
        submit_capture.clear()
        submit_capture.update(args)
        return wrap_text({"recorded": True, "instruction": "MR submission recorded."})

    return _submit_mr


__all__ = ["TOOL_GROUP", "_SUBMIT_MR_SCHEMA", "build"]
