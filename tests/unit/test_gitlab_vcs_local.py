"""Local-git flows of GitLabVcs exercised against a real tmp repo.

We drive the adapter *without* touching the GitLab API. The remote API
methods aren't covered here — those need either live creds or a record/
replay HTTP fixture, which is out of scope for Phase 2 unit tests.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from virtual_dev.adapters.vcs import GitIdentity, GitLabVcs, VcsError
from virtual_dev.infrastructure.config.schema import (
    AgentsCfg,
    AppConfig,
    MappingsCfg,
    RepositoryCfg,
)


def _init_remote_repo(path: Path) -> None:
    """Create a bare-like upstream with a default branch and one commit."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=o@x", "-c", "user.name=o",
                    "commit", "--allow-empty", "-qm", "init"], cwd=path, check=True)
    # Allow fetching into a non-bare repo for test simplicity.
    subprocess.run(["git", "config", "receive.denyCurrentBranch", "updateInstead"],
                   cwd=path, check=True)


def _cfg(repo_key: str, upstream: Path) -> AppConfig:
    return AppConfig(
        repositories=[RepositoryCfg(
            key=repo_key,
            url=str(upstream),
            default_branch="main",
        )],
        agents=AgentsCfg(),
        mappings=MappingsCfg(),
    )


def _vcs(tmp_path: Path, cfg: AppConfig) -> GitLabVcs:
    # GitLab client is never reached in the tests below — but the
    # constructor insists on non-empty URL/token. Use placeholder values.
    return GitLabVcs(
        config=cfg,
        gitlab_url="https://gitlab.example",
        gitlab_token="placeholder",
        workspaces_dir=tmp_path / "workspaces",
        identity=GitIdentity(name="Virtual Dev", email="vdev@example"),
    )


@pytest.mark.asyncio
async def test_ensure_clone_creates_workspace(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _init_remote_repo(upstream)
    vcs = _vcs(tmp_path, _cfg("demo", upstream))

    path = await vcs.ensure_clone("demo")
    assert Path(path).name == "demo"
    assert (Path(path) / ".git").is_dir()

    # Idempotent: calling again returns the same path and does not re-clone.
    path2 = await vcs.ensure_clone("demo")
    assert path == path2


@pytest.mark.asyncio
async def test_create_branch_and_commit_and_push(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _init_remote_repo(upstream)
    vcs = _vcs(tmp_path, _cfg("demo", upstream))

    workspace = Path(await vcs.ensure_clone("demo"))
    await vcs.create_branch("demo", "ai-dev/dm-1", "main")

    (workspace / "new.txt").write_text("hello\n")
    sha = await vcs.commit_all("demo", "Add a file")
    assert sha != ""
    # Second commit with nothing staged → empty sha.
    sha2 = await vcs.commit_all("demo", "noop")
    assert sha2 == ""

    await vcs.push("demo", "ai-dev/dm-1")
    # Verify the branch landed on the upstream.
    out = subprocess.run(
        ["git", "branch", "--list", "ai-dev/dm-1"],
        cwd=upstream, check=True, capture_output=True, text=True,
    ).stdout
    assert "ai-dev/dm-1" in out


@pytest.mark.asyncio
async def test_commit_uses_bot_identity(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _init_remote_repo(upstream)
    vcs = _vcs(tmp_path, _cfg("demo", upstream))

    workspace = Path(await vcs.ensure_clone("demo"))
    await vcs.create_branch("demo", "feature/x", "main")
    (workspace / "a.txt").write_text("x")
    await vcs.commit_all("demo", "feat: x")

    out = subprocess.run(
        ["git", "log", "-1", "--format=%an <%ae>"],
        cwd=workspace, check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert out == "Virtual Dev <vdev@example>"


@pytest.mark.asyncio
async def test_fetch_and_checkout_resets_working_tree(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _init_remote_repo(upstream)
    vcs = _vcs(tmp_path, _cfg("demo", upstream))

    workspace = Path(await vcs.ensure_clone("demo"))
    # Make a local stray change.
    (workspace / "stray.txt").write_text("junk")
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(["git", "-c", "user.email=t@x", "-c", "user.name=t",
                    "commit", "-qm", "stray"], cwd=workspace, check=True)

    assert await vcs.current_branch("demo") == "main"
    await vcs.fetch_and_checkout("demo", "main")
    # After reset, the stray commit is gone.
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=workspace, check=True, capture_output=True, text=True,
    ).stdout
    assert "stray" not in log


@pytest.mark.asyncio
async def test_unknown_repo_is_loud(tmp_path: Path) -> None:
    vcs = _vcs(tmp_path, _cfg("demo", tmp_path / "nowhere"))
    with pytest.raises(VcsError):
        await vcs.ensure_clone("no_such_repo")


@pytest.mark.asyncio
async def test_has_uncommitted_changes(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _init_remote_repo(upstream)
    vcs = _vcs(tmp_path, _cfg("demo", upstream))

    workspace = Path(await vcs.ensure_clone("demo"))
    assert await vcs.has_uncommitted_changes("demo") is False
    (workspace / "dirty.txt").write_text("x")
    assert await vcs.has_uncommitted_changes("demo") is True
