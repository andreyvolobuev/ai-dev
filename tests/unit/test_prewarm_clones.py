"""Startup pre-clone helper: clone every configured repo, resiliently."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from virtual_dev.presentation.web.app import _prewarm_repo_clones


class _FakeVcs:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self._fail = fail or set()

    async def ensure_clone(self, repo_key: str) -> str:
        self.calls.append(repo_key)
        if repo_key in self._fail:
            raise RuntimeError(f"clone {repo_key} boom")
        return f"/ws/{repo_key}"


def _container(vcs: Any, keys: list[str]) -> Any:
    repos = [SimpleNamespace(key=k) for k in keys]
    return SimpleNamespace(vcs=vcs, config=SimpleNamespace(repositories=repos))


@pytest.mark.asyncio
async def test_prewarm_clones_every_configured_repo() -> None:
    vcs = _FakeVcs()
    await _prewarm_repo_clones(cast(Any, _container(vcs, ["a", "b", "c"])))
    assert sorted(vcs.calls) == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_prewarm_one_failure_does_not_abort_the_rest() -> None:
    # A single repo failing to clone (unreachable / bad url) must not stop the
    # others — the Analyst degrades gracefully only for the repo that failed.
    vcs = _FakeVcs(fail={"b"})
    await _prewarm_repo_clones(cast(Any, _container(vcs, ["a", "b", "c"])))
    assert sorted(vcs.calls) == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_prewarm_noop_when_vcs_absent() -> None:
    # Offline/dev stack (no GitLab creds) → vcs is None → nothing to do.
    container = SimpleNamespace(vcs=None, config=SimpleNamespace(repositories=[]))
    await _prewarm_repo_clones(cast(Any, container))
