"""Tool framework for the task-driven clarification subsystem.

A ``Tool`` is the unit the planner picks at each step. Three modes:

* ``SYNC`` — handler returns a payload immediately (e.g.
  ``find_mm_user_by_name`` queries MM and returns candidates). The
  orchestrator routes the payload through the validator on the same
  tick.
* ``ASYNC`` — handler initiates a human-facing conversation (DM,
  channel post). The handler returns a ``PendingReply`` describing
  who/where the bot is now waiting on. Validator runs after the reply
  coalesces.
* ``META`` — handler mutates the task tree directly. Two built-ins:
  ``decompose`` creates child tasks; ``escalate_to_lead`` and
  ``abandon`` close the task.

Tools live alongside the existing :mod:`virtual_dev.application.services.skills`
registry — they're a higher-level concept (one tool may be backed by
one or more skills), but the registration mechanism is parallel and
discoverable in the same `register_user`-style way.

Every tool gets a ``ToolContext`` containing the running task, its
ancestor chain, communicator, plan-row id, and config. Handlers
should be small: most logic stays in the orchestrator's run loop.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loguru import logger

from virtual_dev.domain.models.clarification_task import (
    ClarificationTask,
    ToolMode,
    ToolResult,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from virtual_dev.application.services.communicator import CommunicatorService
    from virtual_dev.infrastructure.config import AppConfig


@dataclass
class ToolContext:
    """Runtime deps + the live task & chain for a tool invocation."""

    task: ClarificationTask
    chain: list[ClarificationTask]   # root → … → parent → task (inclusive)
    communicator: "CommunicatorService"
    config: "AppConfig"
    session_factory: "async_sessionmaker[AsyncSession] | None" = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class PendingReply:
    """Returned by ASYNC tools so the orchestrator knows what to wait on."""

    target_user_id: str
    target_username: str | None
    channel_id: str
    asked_post_id: str
    asked_text: str
    dedupe_key: str | None = None
    info_source: str | None = None
    info_source_class: str | None = None


@dataclass
class ToolOutcome:
    """What a tool handler returns."""

    mode: ToolMode
    # SYNC: validator runs on this.
    result: ToolResult | None = None
    # ASYNC: orchestrator installs awaiting_* on the task.
    pending: PendingReply | None = None
    # META: the orchestrator inspects ``meta_action`` to decide what
    # to do (decompose / escalate / abandon).
    meta_action: str | None = None
    meta_payload: dict[str, Any] = field(default_factory=dict)
    # Common: a short reasoning the orchestrator records as the
    # TOOL_INVOKED step.
    reasoning: str = ""
    # If the tool itself failed (couldn't even produce data), this is
    # surfaced to the planner on the next tick as a "tools_tried"
    # entry but with a failure reason.
    error: str | None = None


ToolHandler = Callable[
    [dict[str, Any], ToolContext], Awaitable[ToolOutcome],
]


@dataclass
class Tool:
    """One pluggable action the planner can pick.

    ``mode`` decides how the orchestrator runs the handler. See module
    docstring.
    """

    name: str
    description: str
    schema: dict[str, Any]
    mode: ToolMode
    handler: ToolHandler
    # Tags in the same vein as Skill.tags — for now every tool is
    # shown to the planner; future agents can filter.
    tags: frozenset[str] = field(default_factory=frozenset)
    source: str = ""


class ToolRegistry:
    """Process-wide list of available tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            existing = self._tools[tool.name]
            if existing.source != tool.source:
                logger.warning(
                    "ToolRegistry: replacing tool {!r} from {!r} with one from {!r}",
                    tool.name, existing.source, tool.source,
                )
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def filter(self, *, tag: str | None = None) -> list[Tool]:
        if tag is None:
            return self.all()
        return [t for t in self._tools.values() if tag in t.tags]

    def clear(self) -> None:
        self._tools.clear()


_REGISTRY = ToolRegistry()


def get_tool_registry() -> ToolRegistry:
    return _REGISTRY


def tool_(
    name: str,
    description: str,
    schema: dict[str, Any],
    mode: ToolMode,
    *,
    tags: frozenset[str] | set[str] | tuple[str, ...] = frozenset(),
) -> Callable[[ToolHandler], ToolHandler]:
    """Decorator to register a tool from a module."""

    def _decorate(fn: ToolHandler) -> ToolHandler:
        module = getattr(fn, "__module__", "<unknown>")
        _REGISTRY.register(Tool(
            name=name,
            description=description,
            schema=schema,
            mode=mode,
            handler=fn,
            tags=frozenset(tags),
            source=module,
        ))
        return fn

    return _decorate


def discover_builtin_tools() -> ToolRegistry:
    """Import every module under :mod:`virtual_dev.skills.tools` so
    the @tool_ decorators register their handlers. Idempotent —
    reload on re-discover.
    """
    import importlib
    import pkgutil
    import sys

    import virtual_dev.skills.tools as pkg

    for _finder, mod_name, is_pkg in pkgutil.iter_modules(pkg.__path__):
        if is_pkg or mod_name.startswith("_"):
            continue
        full_name = f"{pkg.__name__}.{mod_name}"
        try:
            existing = sys.modules.get(full_name)
            if existing is None:
                importlib.import_module(full_name)
            else:
                importlib.reload(existing)
        except Exception:
            logger.exception(
                "ToolRegistry: failed to import tool module {!r}", full_name,
            )
    return _REGISTRY


__all__ = [
    "PendingReply",
    "Tool",
    "ToolContext",
    "ToolHandler",
    "ToolOutcome",
    "ToolRegistry",
    "discover_builtin_tools",
    "get_tool_registry",
    "tool_",
]
