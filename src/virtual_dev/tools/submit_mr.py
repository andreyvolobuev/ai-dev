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
        # Iteration-only: replace the OPEN MR's metadata in GitLab. On an
        # iteration `title` is just the commit message — these are the only
        # way to honour reviewer asks like "поправь заголовок MR". The
        # values are used verbatim (no ticket prefix is prepended).
        "mr_title": {"type": "string"},
        "mr_description": {"type": "string"},
    },
    "required": ["title", "description", "status"],
}


def build(ctx: ToolContext):
    if ctx.submit_capture is None or ctx.run_state is None:
        return None
    submit_capture = ctx.submit_capture
    run_state = ctx.run_state

    @tool(
        "submit_mr",
        "Call this exactly once at the end. Provide the MR title and "
        "a SHORT description of what was done — one 2-3 sentence "
        "paragraph plus at most 5 bullet points, no headings; humans "
        "complain about wall-of-text descriptions. Status is 'success' "
        "if the change is ready for review, 'failed' if you couldn't "
        "complete the task. The runtime commits + pushes + opens the "
        "draft MR for you — don't shell out to git yourself. When "
        "iterating on an existing MR, `title` only names the commit; "
        "to change the MR's actual title/description in GitLab (e.g. "
        "the reviewer asked to rename it), pass `mr_title` / "
        "`mr_description` — they are applied verbatim.",
        _SUBMIT_MR_SCHEMA,
    )
    async def _submit_mr(args: dict[str, Any]) -> dict[str, Any]:
        # Guard against double-call. A hallucinating model that calls
        # submit_mr twice would otherwise silently overwrite the first
        # capture; we treat the first as authoritative.
        if run_state.get("terminal"):
            return wrap_text({"recorded": False, "reason": "already_terminal"})
        submit_capture.clear()
        submit_capture.update(args)
        run_state["terminal"] = True
        return wrap_text({"recorded": True, "instruction": "MR submission recorded."})

    return _submit_mr


__all__ = ["TOOL_GROUP", "_SUBMIT_MR_SCHEMA", "build"]
