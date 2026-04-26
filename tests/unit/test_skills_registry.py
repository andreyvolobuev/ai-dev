"""Tests for the pluggable skill registry.

Pins the contract that:
* ``@skill`` registers a function into the process-wide registry on
  module import.
* ``discover_builtin_skills`` imports every module in
  :mod:`virtual_dev.skills.builtin` and returns a populated registry
  containing every decorated handler.
* ``filter(tag=...)`` returns only skills tagged for that audience.
* ``build_skills_mcp_server`` produces the MCP server + allowed-tool
  names that the planner agent plugs into ``CodeAgentRequest``.
* Skills get the ``SkillContext`` injected at call time and can reach
  CommunicatorService through it.
"""

from __future__ import annotations

from typing import Any

import pytest

from virtual_dev.application.services.communicator import CommunicatorService
from virtual_dev.application.services.injection_filter import InjectionFilter
from virtual_dev.application.services.skills import (
    SkillContext,
    build_skills_mcp_server,
    discover_builtin_skills,
    get_registry,
    skill,
)
from virtual_dev.infrastructure.config.schema import (
    AgentsCfg,
    AppConfig,
    MappingsCfg,
    RepositoryCfg,
)


def _ctx() -> SkillContext:
    return SkillContext(
        communicator=CommunicatorService(None, InjectionFilter()),
        config=AppConfig(
            repositories=[RepositoryCfg(key="x", url="git@x:x.git")],
            agents=AgentsCfg(),
            mappings=MappingsCfg(),
        ),
    )


def test_decorator_registers_handler() -> None:
    registry = get_registry()
    registry.clear()

    @skill(
        name="hello_skill",
        description="say hello",
        schema={"type": "object", "properties": {}},
        tags={"planner"},
    )
    async def _hello(args: dict[str, Any], ctx: SkillContext) -> dict[str, Any]:
        return {"greeting": "hi"}

    names = [s.name for s in registry.all()]
    assert "hello_skill" in names
    matched = next(s for s in registry.all() if s.name == "hello_skill")
    assert "planner" in matched.tags


def test_filter_by_tag_excludes_other_tags() -> None:
    registry = get_registry()
    registry.clear()

    @skill(
        name="planner_only",
        description="planner only",
        schema={"type": "object", "properties": {}},
        tags={"planner"},
    )
    async def _p(args: dict[str, Any], ctx: SkillContext) -> dict[str, Any]:
        return {}

    @skill(
        name="analyst_only",
        description="analyst only",
        schema={"type": "object", "properties": {}},
        tags={"analyst"},
    )
    async def _a(args: dict[str, Any], ctx: SkillContext) -> dict[str, Any]:
        return {}

    planner_skills = registry.filter(tag="planner")
    analyst_skills = registry.filter(tag="analyst")
    assert {s.name for s in planner_skills} == {"planner_only"}
    assert {s.name for s in analyst_skills} == {"analyst_only"}


def test_discover_builtin_finds_lookup_and_search_skills() -> None:
    """The two ship-built-in tools (lookup_mm_user, search_mm_users_by_name)
    must be discoverable without manual import."""
    registry = get_registry()
    registry.clear()
    populated = discover_builtin_skills()
    names = {s.name for s in populated.all()}
    assert "lookup_mm_user" in names
    assert "search_mm_users_by_name" in names


def test_build_skills_mcp_server_returns_allowed_tool_names() -> None:
    """``build_skills_mcp_server`` returns the MCP server + the list of
    allowed tool names the agent must add to CodeAgentRequest.extras."""
    registry = get_registry()
    registry.clear()
    discover_builtin_skills()
    planner_skills = registry.filter(tag="planner")
    server, allowed = build_skills_mcp_server(
        planner_skills, _ctx(), server_name="virtual_dev_skills",
    )
    assert "mcp__virtual_dev_skills__lookup_mm_user" in allowed
    assert "mcp__virtual_dev_skills__search_mm_users_by_name" in allowed
    assert server is not None


def test_build_skills_mcp_server_returns_empty_list_when_no_skills() -> None:
    server, allowed = build_skills_mcp_server([], _ctx())
    assert allowed == []


@pytest.mark.asyncio
async def test_skill_handler_receives_context_with_communicator() -> None:
    """A skill handler can reach CommunicatorService via SkillContext —
    so it can call resolve_user_id / search_users_by_name without the
    skill module knowing how DI is wired."""
    registry = get_registry()
    registry.clear()
    captured: dict[str, Any] = {}

    @skill(
        name="check_ctx",
        description="verifies the ctx object",
        schema={"type": "object", "properties": {}},
        tags={"planner"},
    )
    async def _check(args: dict[str, Any], ctx: SkillContext) -> dict[str, Any]:
        captured["communicator_set"] = ctx.communicator is not None
        captured["config_set"] = ctx.config is not None
        return {"ok": True}

    handler = registry.all()[0].handler
    result = await handler({}, _ctx())
    assert result == {"ok": True}
    assert captured["communicator_set"] is True
    assert captured["config_set"] is True
