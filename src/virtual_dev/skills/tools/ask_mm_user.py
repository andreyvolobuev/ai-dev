"""ASYNC tool: DM a specific Mattermost user with the planner's question."""

from __future__ import annotations

from typing import Any

from virtual_dev.application.services.clarification.tools import (
    PendingReply,
    ToolContext,
    ToolMode,
    ToolOutcome,
    tool_,
)


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "to_handle": {"type": ["string", "null"]},
        "to_email": {"type": ["string", "null"]},
        "message": {
            "type": "string",
            "description": (
                "Body of the DM, freshly composed for THIS recipient "
                "in Russian (or the issue's language). Mention the "
                "ticket number, what you need, and why. ~200-500 chars."
            ),
        },
        "dedupe_key": {
            "type": ["string", "null"],
            "description": (
                "Short semantic key like 'reporter:vasya-handle'. The "
                "orchestrator uses it to detect duplicate asks."
            ),
        },
    },
    "required": ["message"],
}


@tool_(
    name="ask_mm_user",
    description=(
        "DM a specific Mattermost user one question. Pass to_handle or "
        "to_email (one of them); the orchestrator resolves to an id "
        "and refuses if neither matches a real user. The message is "
        "sent verbatim — write it the way the bot should sound."
    ),
    schema=_SCHEMA,
    mode=ToolMode.ASYNC,
    tags={"clarification"},
)
async def ask_mm_user(
    args: dict[str, Any], ctx: ToolContext,
) -> ToolOutcome:
    handle = (args.get("to_handle") or "").strip().lstrip("@") or None
    email = (args.get("to_email") or "").strip() or None
    message = str(args.get("message") or "").strip()
    if not message:
        return ToolOutcome(
            mode=ToolMode.ASYNC, error="empty_message",
        )
    if not handle and not email:
        return ToolOutcome(
            mode=ToolMode.ASYNC, error="missing_target",
        )
    user_id = await ctx.communicator.resolve_user_id(
        username=handle, email=email,
    )
    if user_id is None:
        label = handle or email or ""
        return ToolOutcome(
            mode=ToolMode.ASYNC,
            error=f"target_unresolved:{label}",
        )

    outcome = await ctx.communicator.send_dm(user_id, message)
    if not outcome.sent or outcome.message is None:
        return ToolOutcome(
            mode=ToolMode.ASYNC,
            error=f"send_failed:{outcome.skip_reason or 'unknown'}",
        )

    return ToolOutcome(
        mode=ToolMode.ASYNC,
        pending=PendingReply(
            target_user_id=user_id,
            target_username=handle,
            channel_id=outcome.message.channel_id,
            asked_post_id=outcome.message.id,
            asked_text=message,
            dedupe_key=(args.get("dedupe_key") or None),
            info_source=handle or email,
            info_source_class="mattermost",
        ),
    )
