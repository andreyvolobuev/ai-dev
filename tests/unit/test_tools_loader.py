"""Tests for the tools/ auto-discovery loader.

Pins:
* Files starting with ``_`` are NOT picked up (they're internals).
* Modules without ``build()`` are silently skipped.
* ``build()`` returning None means "skip" (e.g. optional dep absent).
* Each tool module's ``TOOL_GROUP`` constant routes it to a per-group
  MCP server; default group is "analyst".
* Allow-list contains fully-qualified names ``mcp__<server>__<tool>``.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from claude_agent_sdk import tool

from virtual_dev.tools import (
    ToolContext,
    build_tool_servers,
    discover_tools,
    render_tools_catalog,
)


@pytest.fixture
def fake_pkg(monkeypatch: pytest.MonkeyPatch) -> str:
    """Build a synthetic package on the fly so we can test the loader
    in isolation from the real ``virtual_dev.tools`` content. Each
    test gets a fresh package with whatever modules it needs."""
    pkg_name = "_test_tools_pkg"
    if pkg_name in sys.modules:
        del sys.modules[pkg_name]
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = []  # marks it as a package; iter_modules sees no submodules
    sys.modules[pkg_name] = pkg
    yield pkg_name
    # Cleanup: drop everything we added under this prefix.
    for name in list(sys.modules):
        if name == pkg_name or name.startswith(f"{pkg_name}."):
            del sys.modules[name]


def _install_module(
    pkg_name: str, mod_name: str, *, build: Any = None, group: str | None = None,
    has_build: bool = True,
) -> None:
    """Inject a fake submodule into the given package via sys.modules."""
    mod = types.ModuleType(f"{pkg_name}.{mod_name}")
    if has_build:
        mod.build = build
    if group is not None:
        mod.TOOL_GROUP = group
    sys.modules[f"{pkg_name}.{mod_name}"] = mod
    pkg = sys.modules[pkg_name]
    setattr(pkg, mod_name, mod)
    # Make iter_modules see it.
    pkg.__path__ = pkg.__path__ + ["fake"]
    # iter_modules walks __path__; we monkeypatch around it via a
    # hook below in discover_tools_iters_via_modules.


def _make_tool(name: str = "say_hi"):
    @tool(name, "desc", {"type": "object"})
    async def _impl(args: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "hi"}]}
    return _impl


def test_discover_skips_underscore_modules(
    fake_pkg: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def stub_walk(_pkg_path: list[str]) -> list[tuple[Any, str, bool]]:
        return [(None, "_helper", False), (None, "real_tool", False)]

    monkeypatch.setattr(
        "virtual_dev.tools._loader._iter_module_names",
        lambda pkg: [m for _, m, _ in stub_walk([])],
    )

    def real_build(ctx: ToolContext):
        seen.append("real_tool")
        return _make_tool("greet")

    _install_module(fake_pkg, "_helper", build=lambda ctx: _make_tool("HELPER"))
    _install_module(fake_pkg, "real_tool", build=real_build)

    ctx = ToolContext()
    groups = discover_tools(ctx, package_name=fake_pkg)

    assert seen == ["real_tool"], "underscore-prefixed module should be skipped"
    assert "analyst" in groups
    assert [t.name for t in groups["analyst"]] == ["greet"]


def test_module_without_build_is_silently_skipped(
    fake_pkg: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "virtual_dev.tools._loader._iter_module_names",
        lambda pkg: ["no_build", "with_build"],
    )
    _install_module(fake_pkg, "no_build", has_build=False)
    _install_module(
        fake_pkg, "with_build", build=lambda ctx: _make_tool("ok"),
    )
    groups = discover_tools(ToolContext(), package_name=fake_pkg)
    assert [t.name for t in groups["analyst"]] == ["ok"]


def test_build_returning_none_skips_the_tool(
    fake_pkg: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool can declare itself unavailable (e.g. optional dep is None
    in ctx) by returning None from build()."""
    monkeypatch.setattr(
        "virtual_dev.tools._loader._iter_module_names",
        lambda pkg: ["needs_kb", "always_on"],
    )
    _install_module(fake_pkg, "needs_kb", build=lambda ctx: None)
    _install_module(
        fake_pkg, "always_on", build=lambda ctx: _make_tool("always"),
    )
    groups = discover_tools(ToolContext(), package_name=fake_pkg)
    assert [t.name for t in groups["analyst"]] == ["always"]


def test_tool_group_routes_to_distinct_mcp_server(
    fake_pkg: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "virtual_dev.tools._loader._iter_module_names",
        lambda pkg: ["one", "two"],
    )
    _install_module(
        fake_pkg, "one", build=lambda ctx: _make_tool("a"), group="shared",
    )
    _install_module(
        fake_pkg, "two", build=lambda ctx: _make_tool("b"), group="analyst",
    )
    servers, allowed, groups = build_tool_servers(
        ToolContext(), package_name=fake_pkg,
    )
    assert "virtual_dev_shared" in servers
    assert "virtual_dev_analyst" in servers
    assert "mcp__virtual_dev_shared__a" in allowed
    assert "mcp__virtual_dev_analyst__b" in allowed
    # Groups are exposed verbatim so the caller can introspect tool
    # ``.name`` / ``.description`` for catalogue rendering.
    assert {"shared", "analyst"} <= set(groups)
    assert [t.name for t in groups["shared"]] == ["a"]
    assert [t.name for t in groups["analyst"]] == ["b"]


def test_render_tools_catalog_groups_and_lists(
    fake_pkg: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catalogue exposes one bullet per discovered tool with its
    description verbatim. Adding a ``tools/<file>.py`` is then enough
    to surface the new tool in the analyst's system prompt — no manual
    edit to ``analyst.md`` required."""
    monkeypatch.setattr(
        "virtual_dev.tools._loader._iter_module_names",
        lambda pkg: ["alpha", "beta"],
    )

    @tool("alpha", "Reads things from somewhere.", {"type": "object"})
    async def _alpha(args: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "ok"}]}

    @tool("beta", "Writes things back.", {"type": "object"})
    async def _beta(args: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "ok"}]}

    _install_module(fake_pkg, "alpha", build=lambda ctx: _alpha, group="shared")
    _install_module(fake_pkg, "beta", build=lambda ctx: _beta, group="analyst")

    _, _, groups = build_tool_servers(ToolContext(), package_name=fake_pkg)
    catalog = render_tools_catalog(groups, extra_builtins="**Builtins**: foo, bar.")

    # Shared group rendered first (declared in _GROUP_HEADERS), then analyst.
    assert catalog.index("Shared tools") < catalog.index("Analyst tools")
    assert "* `alpha` — Reads things from somewhere." in catalog
    assert "* `beta` — Writes things back." in catalog
    assert catalog.endswith("**Builtins**: foo, bar.")
