"""Researcher data bundle — passed to research tools via ``ToolContext``.

Tool implementations live in :mod:`virtual_dev.tools` (one file per
tool). This module just holds the bundle of dependencies they share:

* a map of configured repos to their local checkouts;
* the optional knowledge-base adapter;
* the optional MR-history adapter;
* the injection filter that wraps untrusted tool output.

There are no tool implementations or MCP-server factories here anymore.
A tool that wants to grep code reads ``ctx.researcher.repos`` directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from virtual_dev.application.services.injection_filter import InjectionFilter
from virtual_dev.domain.ports.knowledge_base import KnowledgeBasePort
from virtual_dev.domain.ports.mr_history import MrHistoryPort
from virtual_dev.domain.ports.vcs import VcsPort
from virtual_dev.infrastructure.config import AppConfig


@dataclass
class RepoHandle:
    """Resolved pointer to a local checkout of a repository."""

    key: str
    local_path: Path


def _build_repo_handles(config: AppConfig, workspaces_dir: str | Path) -> dict[str, RepoHandle]:
    handles: dict[str, RepoHandle] = {}
    ws_root = Path(workspaces_dir)
    for repo in config.repositories:
        if repo.local_path:
            path = Path(repo.local_path)
        else:
            path = ws_root / repo.key
        handles[repo.key] = RepoHandle(key=repo.key, local_path=path)
    return handles


class ResearcherToolkit:
    """Bundle of research dependencies. Read-only after construction.

    Tools access ``repos`` / ``kb`` / ``mr_history`` / ``filter``
    directly. Defaults for grep / file-read caps live here so multiple
    tools share them.
    """

    DEFAULT_MAX_GREP_RESULTS = 30
    DEFAULT_MAX_FILE_BYTES = 12_000

    def __init__(
        self,
        *,
        config: AppConfig,
        workspaces_dir: str | Path,
        knowledge_base: KnowledgeBasePort | None,
        injection_filter: InjectionFilter,
        mr_history: MrHistoryPort | None = None,
        vcs: VcsPort | None = None,
    ) -> None:
        self._repos = _build_repo_handles(config, workspaces_dir)
        self._kb = knowledge_base
        self._filter = injection_filter
        self._mr_history = mr_history
        self._vcs = vcs
        self._config = config

    @property
    def repos(self) -> dict[str, RepoHandle]:
        return self._repos

    @property
    def kb(self) -> KnowledgeBasePort | None:
        return self._kb

    @property
    def mr_history(self) -> MrHistoryPort | None:
        return self._mr_history

    @property
    def filter(self) -> InjectionFilter:
        return self._filter

    @property
    def vcs(self) -> VcsPort | None:
        return self._vcs

    @property
    def config(self) -> AppConfig:
        return self._config
