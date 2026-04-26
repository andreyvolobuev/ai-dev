"""META tool: split the current task into one or more sub-tasks."""

from __future__ import annotations

from typing import Any

from virtual_dev.application.services.clarification.tools import (
    ToolContext,
    ToolMode,
    ToolOutcome,
    tool_,
)


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "subtasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "info_source": {"type": ["string", "null"]},
                    "info_source_class": {"type": ["string", "null"]},
                },
                "required": ["question"],
            },
            "description": (
                "One or more child tasks. Each child becomes its own "
                "ClarificationTask with its own loop; the parent "
                "task waits until every child is solved or terminal."
            ),
        },
    },
    "required": ["subtasks"],
}


@tool_(
    name="decompose",
    description=(
        "Decompose the current task into one or more child tasks. Use "
        "this when answering the current task requires learning "
        "something else first (e.g. the question is 'get body example "
        "from Vasya' — child = 'find Vasya's MM handle'). Don't "
        "decompose for the sake of decomposition: if a single SYNC "
        "tool plus a DM is enough, that's not decomposition. Each "
        "subtask must be self-contained — its question is read by the "
        "child's planner cold, without parent context."
    ),
    schema=_SCHEMA,
    mode=ToolMode.META,
    tags={"clarification"},
)
async def decompose(
    args: dict[str, Any], ctx: ToolContext,
) -> ToolOutcome:
    raw = args.get("subtasks") or []
    subtasks: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            question = str(entry.get("question") or "").strip()
            if not question:
                continue
            subtasks.append({
                "question": question,
                "info_source": (
                    (entry.get("info_source") or "").strip() or None
                ),
                "info_source_class": (
                    (entry.get("info_source_class") or "").strip() or None
                ),
            })
    if not subtasks:
        return ToolOutcome(mode=ToolMode.META, error="no_subtasks")
    return ToolOutcome(
        mode=ToolMode.META,
        meta_action="decompose",
        meta_payload={"subtasks": subtasks},
    )
