"""SYNC tool: resolve a Mattermost user by exact handle or email."""

from __future__ import annotations

from typing import Any

from virtual_dev.application.services.clarification.tools import (
    ToolContext,
    ToolMode,
    ToolOutcome,
    tool_,
)
from virtual_dev.domain.models.clarification_task import ToolResult


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "handle": {"type": ["string", "null"]},
        "email": {"type": ["string", "null"]},
    },
}


@tool_(
    name="lookup_mm_user",
    description=(
        "Resolve a Mattermost user by exact handle or email. Use AFTER "
        "find_mm_user_by_name has narrowed the candidate, or when a "
        "human DM'd you the @-handle — it's a sanity check that the "
        "handle resolves to a real id."
    ),
    schema=_SCHEMA,
    mode=ToolMode.SYNC,
    tags={"clarification"},
)
async def lookup_mm_user(
    args: dict[str, Any], ctx: ToolContext,
) -> ToolOutcome:
    handle = (args.get("handle") or "").strip().lstrip("@") or None
    email = (args.get("email") or "").strip() or None
    if not handle and not email:
        return ToolOutcome(
            mode=ToolMode.SYNC,
            error="missing_handle_and_email",
            result=ToolResult(
                text="No handle or email supplied — pass one.",
                source_class="mattermost",
            ),
        )
    user_id = await ctx.communicator.resolve_user_id(
        username=handle, email=email,
    )
    label = handle or email or ""
    if user_id is None:
        return ToolOutcome(
            mode=ToolMode.SYNC,
            result=ToolResult(
                text=f"Mattermost: handle={handle!r} email={email!r} did NOT resolve.",
                structured={"found": False, "handle": handle, "email": email},
                source_class="mattermost",
                source_label=label,
            ),
        )
    return ToolOutcome(
        mode=ToolMode.SYNC,
        result=ToolResult(
            text=(
                f"Mattermost: {label!r} resolves to mm_user_id={user_id}."
            ),
            structured={
                "found": True, "handle": handle, "email": email,
                "mm_user_id": user_id,
            },
            source_class="mattermost",
            source_label=label,
        ),
    )
