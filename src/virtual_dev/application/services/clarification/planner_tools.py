"""MCP tools the ClarificationPlanner exposes to its Claude session.

Currently a single tool — ``lookup_mm_user`` — wrapping
``CommunicatorService.resolve_user_id``. The planner uses it to decide
whether a free-form name (e.g. "Вася Курочкин") is reachable in
Mattermost without first DM-ing a human to ask.

The factory returns a (mcp_servers, allowed_tool_names) tuple that
the agent slots into ``CodeAgentRequest.extras`` alongside the
researcher's existing tools.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig  # type: ignore[attr-defined]
from loguru import logger

from virtual_dev.application.services.communicator import CommunicatorService

_LOOKUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "handle": {"type": ["string", "null"]},
        "email": {"type": ["string", "null"]},
    },
}


def build_planner_mcp_server(
    communicator: CommunicatorService,
) -> tuple[McpSdkServerConfig, list[str]]:
    """Build the MCP server hosting the planner's ``lookup_mm_user`` tool.

    Returned ``allowed_tool_names`` MUST be added to the request's
    extras so Claude is permitted to call them.
    """

    @tool(
        "lookup_mm_user",
        "Resolve a Mattermost user by handle or email. "
        "Returns {found: bool, mm_user_id?: str, display_name?: str}. "
        "Use BEFORE asking a human about a person whose existence in "
        "Mattermost is uncertain — saves a DM round-trip.",
        _LOOKUP_SCHEMA,
    )
    async def _lookup_mm_user(args: dict[str, Any]) -> dict[str, Any]:
        handle = (args.get("handle") or "").strip().lstrip("@") or None
        email = (args.get("email") or "").strip() or None
        if not handle and not email:
            return _wrap({
                "found": False,
                "reason": "no_handle_or_email_provided",
            })
        try:
            user_id = await communicator.resolve_user_id(
                username=handle, email=email,
            )
        except Exception as exc:
            logger.exception(
                "lookup_mm_user: resolve_user_id raised for handle={!r}, email={!r}",
                handle, email,
            )
            return _wrap({"found": False, "reason": f"error: {type(exc).__name__}"})
        if user_id is None:
            return _wrap({"found": False, "reason": "not_found"})
        return _wrap({
            "found": True,
            "mm_user_id": user_id,
            "display_name": handle or email,
        })

    server = create_sdk_mcp_server(
        name="virtual_dev_planner_tools", version="0.1.0",
        tools=[_lookup_mm_user],
    )
    return server, ["mcp__virtual_dev_planner_tools__lookup_mm_user"]


def _wrap(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a dict result into the SDK's tool-response shape."""
    import json

    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


__all__ = ["build_planner_mcp_server"]
