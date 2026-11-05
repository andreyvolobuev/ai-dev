"""Remote Professor LLM-Wiki MCP helper tests."""

from __future__ import annotations

import os
from typing import Any
from unittest import mock

import pytest

from virtual_dev.infrastructure.config import Settings
from virtual_dev.tools import (
    PROFESSOR_SERVER_KEY,
    PROFESSOR_WIKI_GET_PAGE_TOOL,
    PROFESSOR_WIKI_SEARCH_TOOL,
    append_professor_catalog,
    merge_professor_mcp,
    professor_catalog_extra,
    professor_mcp_server,
    professor_tool_names,
)


def _settings(url: str = "", token: str = "") -> Settings:
    env = {
        "PROFESSOR_MCP_URL": url,
        "PROFESSOR_MCP_TOKEN": token,
    }
    with mock.patch.dict(os.environ, env, clear=False):
        return Settings()


def test_disabled_when_both_values_are_empty() -> None:
    settings = _settings()

    assert professor_mcp_server(settings) == {}
    assert professor_tool_names(settings) == []
    assert professor_catalog_extra(settings) == ""


@pytest.mark.parametrize(
    ("url", "token"),
    [
        ("", "token"),
        ("https://professor.example/mcp", ""),
        ("   ", "token"),
        ("https://professor.example/mcp", "   "),
    ],
)
def test_partial_config_is_rejected(url: str, token: str) -> None:
    with pytest.raises(ValueError, match="configure both or neither"):
        professor_mcp_server(_settings(url=url, token=token))


def test_configured_server_uses_http_and_bearer() -> None:
    settings = _settings(
        url="https://ai-professor-dm-ai-magic.istio.k8s.2gis.dev/mcp",
        token="prof-token",
    )

    server = professor_mcp_server(settings)
    assert set(server) == {PROFESSOR_SERVER_KEY}
    assert server[PROFESSOR_SERVER_KEY]["type"] == "http"
    assert server[PROFESSOR_SERVER_KEY]["url"] == (
        "https://ai-professor-dm-ai-magic.istio.k8s.2gis.dev/mcp"
    )
    assert server[PROFESSOR_SERVER_KEY]["headers"] == {
        "Authorization": "Bearer prof-token",
    }

    assert professor_tool_names(settings) == [
        PROFESSOR_WIKI_SEARCH_TOOL,
        PROFESSOR_WIKI_GET_PAGE_TOOL,
    ]


def test_merge_adds_remote_server_without_touching_local_entries() -> None:
    settings = _settings(url="https://professor.example/mcp", token="prof-token")
    local_servers: dict[str, Any] = {
        "virtual_dev_dev": {"type": "sdk", "name": "dev"},
    }
    local_allowed = ["mcp__virtual_dev_dev__submit_mr"]

    servers, allowed = merge_professor_mcp(settings, local_servers, local_allowed)

    assert set(servers) == {"virtual_dev_dev", PROFESSOR_SERVER_KEY}
    assert servers["virtual_dev_dev"] == {"type": "sdk", "name": "dev"}
    assert allowed == [
        "mcp__virtual_dev_dev__submit_mr",
        PROFESSOR_WIKI_SEARCH_TOOL,
        PROFESSOR_WIKI_GET_PAGE_TOOL,
    ]
    # The caller's input maps stay untouched.
    assert local_servers == {"virtual_dev_dev": {"type": "sdk", "name": "dev"}}
    assert local_allowed == ["mcp__virtual_dev_dev__submit_mr"]


def test_merge_is_noop_when_disabled() -> None:
    settings = _settings()
    local_servers: dict[str, Any] = {
        "virtual_dev_dev": {"type": "sdk", "name": "dev"},
    }
    local_allowed = ["mcp__virtual_dev_dev__submit_mr"]

    servers, allowed = merge_professor_mcp(settings, local_servers, local_allowed)

    assert servers == local_servers
    assert allowed == local_allowed


def test_catalog_fragment_lists_both_tools_for_configured_server() -> None:
    settings = _settings(url="https://professor.example/mcp", token="prof-token")
    extra = professor_catalog_extra(settings)

    assert PROFESSOR_WIKI_SEARCH_TOOL in extra
    assert PROFESSOR_WIKI_GET_PAGE_TOOL in extra
    assert "untrusted" in extra.lower()


def test_append_catalog_appends_only_when_configured() -> None:
    configured = _settings(url="https://professor.example/mcp", token="prof-token")
    disabled = _settings()
    catalog = "**Dev tools**: submit_mr"

    merged = append_professor_catalog(configured, catalog)
    assert merged.startswith(catalog)
    assert PROFESSOR_WIKI_SEARCH_TOOL in merged

    assert append_professor_catalog(disabled, catalog) == catalog
