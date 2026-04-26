"""Pluggable skill registry — drop a file, get a tool.

A "skill" is a single tool the planner (and, eventually, other LLM
agents) can call: a name, a description, a JSON-schema, and an async
callable. Skills live as standalone modules under
``virtual_dev.skills.builtin`` (and, later, user-supplied directories).
At startup, ``discover_builtin_skills`` imports each module — the
``@skill`` decorator on each module's function registers it into a
process-wide registry. Agents pull the registry at MCP-build time.

Why this matters: previously, adding a new tool meant editing
``planner_tools.py`` and rebuilding the MCP server inline. With the
registry, you write one self-contained file (e.g.
``skills/builtin/search_jira.py``) and it lights up wherever the
``SkillContext`` has the dependencies it needs. We can later limit
scope by tag (e.g. only-planner, only-analyst) — for now every skill
is offered to every agent that opts in.

Skills get a ``SkillContext`` injected at call time so they can reach
:class:`CommunicatorService`, the DB session factory, etc. without
having to pass dependencies around at registration time. The context
is constructed by the container, not by individual skills.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig  # type: ignore[attr-defined]
from loguru import logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from virtual_dev.application.services.communicator import CommunicatorService
    from virtual_dev.infrastructure.config import AppConfig


@dataclass
class SkillContext:
    """Runtime dependencies available to every skill at call time.

    Add fields here as new skills need them. The container builds one
    SkillContext per process and passes it to ``build_skills_mcp_server``.
    """

    communicator: "CommunicatorService"
    config: "AppConfig"
    session_factory: "async_sessionmaker[AsyncSession] | None" = None


SkillCallable = Callable[
    [dict[str, Any], SkillContext], Awaitable[dict[str, Any]],
]


@dataclass
class Skill:
    """One pluggable tool definition registered into ``SkillRegistry``."""

    name: str
    description: str
    schema: dict[str, Any]
    handler: SkillCallable
    # Optional tags — agents can filter (e.g. only "planner" skills).
    # Empty tags == available to every agent that mounts the registry.
    tags: frozenset[str] = field(default_factory=frozenset)
    # Where the skill came from — for logging / debugging.
    source: str = ""


class SkillRegistry:
    """Process-wide list of skills.

    A single module-level instance (``_REGISTRY``) is mutated by the
    ``@skill`` decorator. Agents pull it via :func:`get_registry` and
    build their MCP server from a filtered subset.
    """

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill_obj: Skill) -> None:
        if skill_obj.name in self._skills:
            existing = self._skills[skill_obj.name]
            if existing.source != skill_obj.source:
                logger.warning(
                    "SkillRegistry: replacing skill {!r} from {!r} with one from {!r}",
                    skill_obj.name, existing.source, skill_obj.source,
                )
        self._skills[skill_obj.name] = skill_obj

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def filter(self, *, tag: str | None = None) -> list[Skill]:
        """Return skills matching ``tag`` (or all if tag is None)."""
        if tag is None:
            return self.all()
        return [s for s in self._skills.values() if tag in s.tags]

    def clear(self) -> None:
        """Tests only: drop everything to start clean."""
        self._skills.clear()


_REGISTRY = SkillRegistry()


def get_registry() -> SkillRegistry:
    return _REGISTRY


def skill(
    name: str,
    description: str,
    schema: dict[str, Any],
    *,
    tags: frozenset[str] | set[str] | tuple[str, ...] = frozenset(),
) -> Callable[[SkillCallable], SkillCallable]:
    """Decorator to register a skill from a module.

    Usage::

        @skill(
            name="lookup_mm_user",
            description="Resolve a Mattermost user by handle or email.",
            schema={...},
            tags={"planner"},
        )
        async def lookup_mm_user(args, ctx):
            ...
    """

    def _decorate(fn: SkillCallable) -> SkillCallable:
        module = getattr(fn, "__module__", "<unknown>")
        _REGISTRY.register(Skill(
            name=name,
            description=description,
            schema=schema,
            handler=fn,
            tags=frozenset(tags),
            source=module,
        ))
        return fn

    return _decorate


def discover_builtin_skills() -> SkillRegistry:
    """Import every module under ``virtual_dev.skills.builtin``.

    The ``@skill`` decorator on each module registers itself when the
    module is imported. Returns the now-populated registry.

    Re-importing is idempotent: if the registry already has the skill,
    the decorator's ``register`` call is a no-op replace. We DO use
    ``importlib.reload`` for already-imported modules so a registry
    that was cleared (in tests) gets re-populated.
    """
    import sys

    import virtual_dev.skills.builtin as pkg

    for _finder, mod_name, is_pkg in pkgutil.iter_modules(pkg.__path__):
        if is_pkg or mod_name.startswith("_"):
            continue
        full_name = f"{pkg.__name__}.{mod_name}"
        try:
            existing = sys.modules.get(full_name)
            if existing is None:
                importlib.import_module(full_name)
            else:
                # Already imported — reload so the @skill decorators
                # re-fire (cheap; modules are tiny).
                importlib.reload(existing)
        except Exception:
            logger.exception(
                "SkillRegistry: failed to import builtin skill {!r}", full_name,
            )
    return _REGISTRY


def build_skills_mcp_server(
    skills: list[Skill],
    context: SkillContext,
    *,
    server_name: str = "virtual_dev_skills",
) -> tuple[McpSdkServerConfig, list[str]]:
    """Build a single MCP server hosting ``skills`` for an agent.

    Returns ``(server, allowed_tool_names)`` so the caller can plug them
    into ``CodeAgentRequest.extras``.
    """
    if not skills:
        return _empty_server(server_name), []

    sdk_tools: list[Any] = []
    allowed: list[str] = []
    for s in skills:
        wrapped = _make_sdk_tool(s, context)
        sdk_tools.append(wrapped)
        allowed.append(f"mcp__{server_name}__{s.name}")
    server = create_sdk_mcp_server(
        name=server_name, version="0.1.0", tools=sdk_tools,
    )
    return server, allowed


def _empty_server(server_name: str) -> McpSdkServerConfig:
    return create_sdk_mcp_server(
        name=server_name, version="0.1.0", tools=[],
    )


def _make_sdk_tool(s: Skill, ctx: SkillContext) -> Any:
    """Wrap a Skill into a Claude-Agent-SDK ``@tool``-decorated callable.

    The SDK calls our handler with ``args`` only; we close over ``ctx``
    so the skill receives both. Errors are caught and surfaced as JSON
    so the model sees a graceful failure rather than the agent dying.
    """

    @tool(s.name, s.description, s.schema)
    async def _wrapper(args: dict[str, Any]) -> dict[str, Any]:
        try:
            payload = await s.handler(args, ctx)
        except Exception as exc:
            logger.exception(
                "Skill {!r}: handler raised", s.name,
            )
            payload = {"error": f"{type(exc).__name__}: {exc}"}
        return _wrap_payload(payload)

    return _wrapper


def _wrap_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{
        "type": "text",
        "text": json.dumps(payload, ensure_ascii=False),
    }]}


__all__ = [
    "Skill",
    "SkillContext",
    "SkillRegistry",
    "build_skills_mcp_server",
    "discover_builtin_skills",
    "get_registry",
    "skill",
]
