"""Resolve a chat-platform user by exact handle or email."""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from virtual_dev.tools import ToolContext, wrap_text

TOOL_GROUP = "analyst"


def build(ctx: ToolContext):
    if ctx.communicator is None:
        return None
    communicator = ctx.communicator

    @tool(
        "lookup_chat_user",
        "Resolve a chat-platform user by exact handle or email. "
        "Returns {found: bool, user_id?, display_name?}.",
        {
            "type": "object",
            "properties": {
                "handle": {"type": ["string", "null"]},
                "email": {"type": ["string", "null"]},
            },
        },
    )
    async def _lookup(args: dict[str, Any]) -> dict[str, Any]:
        handle = (args.get("handle") or "").strip().lstrip("@") or None
        email = (args.get("email") or "").strip() or None
        if not handle and not email:
            return wrap_text({"found": False, "reason": "no_handle_or_email"})
        uid = await communicator.resolve_user_id(username=handle, email=email)
        if uid is None:
            return wrap_text({"found": False, "reason": "not_found"})
        return wrap_text({
            "found": True, "user_id": uid,
            "display_name": handle or email,
        })

    return _lookup
