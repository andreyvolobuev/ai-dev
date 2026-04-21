"""Unit tests for ResearcherToolkit — direct tool invocations."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from virtual_dev.application.services import InjectionFilter, ResearcherToolkit
from virtual_dev.domain.models.kb import KBPage
from virtual_dev.domain.ports.knowledge_base import KnowledgeBasePort
from virtual_dev.infrastructure.config.schema import (
    AgentsCfg,
    AppConfig,
    MappingsCfg,
    RepositoryCfg,
)


class _FakeKb(KnowledgeBasePort):
    def __init__(self, pages: dict[str, KBPage]) -> None:
        self._pages = pages

    async def fetch_page(self, page_id: str) -> KBPage:
        return self._pages[page_id]

    async def fetch_page_by_url(self, url: str) -> KBPage:
        # For tests, map URL tail to page id.
        return self._pages[url.rsplit("/", 1)[-1]]

    async def search(self, query: str, limit: int = 10) -> Sequence[KBPage]:
        return [p for p in self._pages.values() if query.lower() in p.content_text.lower()][:limit]


def _cfg(local_path: str) -> AppConfig:
    return AppConfig(
        repositories=[
            RepositoryCfg(
                key="demo",
                url="git@example:demo.git",
                local_path=local_path,
                default_branch="main",
            ),
        ],
        agents=AgentsCfg(),
        mappings=MappingsCfg(),
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "alpha.py").write_text("def alpha():\n    return 'needle-in-alpha'\n")
    (tmp_path / "pkg" / "beta.py").write_text("def beta():\n    return 'boring'\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@e", "-c", "user.name=t",
                    "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@e", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.mark.asyncio
async def test_search_code_finds_matches(git_repo: Path) -> None:
    toolkit = ResearcherToolkit(
        config=_cfg(str(git_repo)),
        workspaces_dir="/tmp",
        knowledge_base=None,
        injection_filter=InjectionFilter(),
    )
    result = await toolkit._run_search_code({"pattern": "needle-in-alpha", "repo_key": "demo"})
    text = result["content"][0]["text"]
    assert "alpha.py" in text
    assert "needle-in-alpha" in text
    # The result is wrapped for Analyst consumption.
    assert "<untrusted_content" in text


@pytest.mark.asyncio
async def test_search_code_reports_missing_repo(tmp_path: Path) -> None:
    toolkit = ResearcherToolkit(
        config=_cfg(str(tmp_path / "does_not_exist")),
        workspaces_dir="/tmp",
        knowledge_base=None,
        injection_filter=InjectionFilter(),
    )
    result = await toolkit._run_search_code({"pattern": "x", "repo_key": "demo"})
    assert result.get("is_error") is True


@pytest.mark.asyncio
async def test_read_file_blocks_path_escape(git_repo: Path) -> None:
    toolkit = ResearcherToolkit(
        config=_cfg(str(git_repo)),
        workspaces_dir="/tmp",
        knowledge_base=None,
        injection_filter=InjectionFilter(),
    )
    result = await toolkit._run_read_file({"path": "../../etc/passwd", "repo_key": "demo"})
    assert result.get("is_error") is True


@pytest.mark.asyncio
async def test_read_file_returns_content(git_repo: Path) -> None:
    toolkit = ResearcherToolkit(
        config=_cfg(str(git_repo)),
        workspaces_dir="/tmp",
        knowledge_base=None,
        injection_filter=InjectionFilter(),
    )
    result = await toolkit._run_read_file({"path": "pkg/alpha.py", "repo_key": "demo"})
    text = result["content"][0]["text"]
    assert "needle-in-alpha" in text


@pytest.mark.asyncio
async def test_kb_search_without_adapter_errors(tmp_path: Path) -> None:
    toolkit = ResearcherToolkit(
        config=_cfg(str(tmp_path)),
        workspaces_dir="/tmp",
        knowledge_base=None,
        injection_filter=InjectionFilter(),
    )
    result = await toolkit._run_kb_search({"query": "x"})
    assert result.get("is_error") is True


@pytest.mark.asyncio
async def test_kb_search_returns_wrapped_results(tmp_path: Path) -> None:
    kb = _FakeKb({
        "p1": KBPage(id="p1", title="Pipeline architecture", url="u1",
                     content_text="This page describes the ingest pipeline."),
    })
    toolkit = ResearcherToolkit(
        config=_cfg(str(tmp_path)),
        workspaces_dir="/tmp",
        knowledge_base=kb,
        injection_filter=InjectionFilter(),
    )
    result = await toolkit._run_kb_search({"query": "ingest"})
    text = result["content"][0]["text"]
    assert "Pipeline architecture" in text
    assert "<untrusted_content" in text
