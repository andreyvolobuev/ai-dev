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
from virtual_dev.adapters.vcs.gitlab import VcsRogueCommitError
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


def _cfg(repo_key: str, upstream: Path | str) -> AppConfig:
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
    # Second call with nothing new staged: branch still ahead of origin
    # (we haven't pushed yet), so commit_all returns the same sha — caller
    # should still push it. HEAD didn't move.
    sha2 = await vcs.commit_all("demo", "noop")
    assert sha2 == sha

    await vcs.push("demo", "ai-dev/dm-1")
    # After push, local matches origin → commit_all reports nothing to push.
    sha3 = await vcs.commit_all("demo", "noop")
    assert sha3 == ""
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
async def test_ensure_clone_repairs_interrupted_clone(tmp_path: Path) -> None:
    """A leftover half-clone (``.git`` exists but HEAD is unresolvable —
    the process was killed mid-clone) must be wiped and re-cloned, not
    returned as a "ready" empty checkout the Analyst then reads blind."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _init_remote_repo(upstream)
    (upstream / "marker.txt").write_text("hello")
    subprocess.run(["git", "-c", "user.email=o@x", "-c", "user.name=o",
                    "add", "-A"], cwd=upstream, check=True)
    subprocess.run(["git", "-c", "user.email=o@x", "-c", "user.name=o",
                    "commit", "-qm", "add marker"], cwd=upstream, check=True)
    vcs = _vcs(tmp_path, _cfg("demo", upstream))

    # Simulate an interrupted `git clone`: .git exists, but no HEAD commit
    # and an empty working tree.
    broken = tmp_path / "workspaces" / "demo"
    (broken / ".git").mkdir(parents=True)
    (broken / ".git" / "config").write_text("[core]\n")

    path = Path(await vcs.ensure_clone("demo"))
    assert (path / "marker.txt").read_text() == "hello"
    head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"],
                          cwd=path, capture_output=True, text=True)
    assert head.returncode == 0


@pytest.mark.asyncio
async def test_ensure_clone_is_shallow_single_branch(tmp_path: Path) -> None:
    """Clones are --depth=1 --single-branch: the bot only needs the default
    branch's current tree (it branches off locally and pushes). Other
    branches are fetched on demand — see the checkout test below.

    The upstream must be a file:// URL — git silently ignores --depth for
    plain local-path clones."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _init_remote_repo(upstream)
    subprocess.run(["git", "-c", "user.email=o@x", "-c", "user.name=o",
                    "commit", "--allow-empty", "-qm", "second"],
                   cwd=upstream, check=True)
    subprocess.run(["git", "branch", "feature"], cwd=upstream, check=True)
    vcs = _vcs(tmp_path, _cfg("demo", f"file://{upstream}"))

    path = Path(await vcs.ensure_clone("demo"))

    depth = subprocess.run(["git", "rev-list", "--count", "HEAD"],
                           cwd=path, capture_output=True, text=True, check=True)
    assert depth.stdout.strip() == "1"
    feature = subprocess.run(["git", "rev-parse", "--verify", "origin/feature"],
                             cwd=path, capture_output=True, text=True)
    assert feature.returncode != 0


@pytest.mark.asyncio
async def test_checkout_existing_branch_fetches_outside_single_branch_refspec(
    tmp_path: Path,
) -> None:
    """After a restart the bot's MR branch is not in the --single-branch
    clone — checkout_existing_branch must fetch it explicitly and check out
    the right tree."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _init_remote_repo(upstream)
    subprocess.run(["git", "checkout", "-qb", "ai-dev/DM-1-fix"], cwd=upstream, check=True)
    (upstream / "fix.txt").write_text("the fix")
    subprocess.run(["git", "add", "-A"], cwd=upstream, check=True)
    subprocess.run(["git", "-c", "user.email=o@x", "-c", "user.name=o",
                    "commit", "-qm", "fix"], cwd=upstream, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=upstream, check=True)
    vcs = _vcs(tmp_path, _cfg("demo", f"file://{upstream}"))

    workspace = Path(await vcs.ensure_clone("demo"))
    await vcs.checkout_existing_branch("demo", "ai-dev/DM-1-fix")

    assert (workspace / "fix.txt").read_text() == "the fix"
    assert await vcs.current_branch("demo") == "ai-dev/DM-1-fix"


@pytest.mark.asyncio
async def test_failed_clone_leaves_no_workspace_dir(tmp_path: Path) -> None:
    """A failed clone must not leave a partial workspace behind: while the
    clone is running (and after it fails) the destination path must not
    exist, so read-only agents never see a half-populated tree."""
    vcs = _vcs(tmp_path, _cfg("demo", tmp_path / "nowhere"))
    with pytest.raises(VcsError):
        await vcs.ensure_clone("demo")
    workspace = tmp_path / "workspaces" / "demo"
    assert not workspace.exists()
    leftovers = list((tmp_path / "workspaces").glob("*demo*"))
    assert leftovers == []


@pytest.mark.asyncio
async def test_unknown_repo_is_loud(tmp_path: Path) -> None:
    vcs = _vcs(tmp_path, _cfg("demo", tmp_path / "nowhere"))
    with pytest.raises(VcsError):
        await vcs.ensure_clone("no_such_repo")


def test_https_push_url_embeds_token_and_host(tmp_path: Path) -> None:
    """The push URL must:
    1. use HTTPS (not the SSH form of the remote in .git/config),
    2. embed the bot's PAT as ``oauth2:<token>@host``,
    3. point at the same project path the SSH URL refers to.

    Pushing through this URL makes GitLab attribute the resulting
    pipeline to the bot account that owns the PAT, instead of to
    whoever's SSH key is loaded in the user's agent."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _init_remote_repo(upstream)
    cfg = AppConfig(
        repositories=[RepositoryCfg(
            key="demo",
            url="git@gitlab.example.com:group/sub/demo.git",
            default_branch="main",
        )],
        agents=AgentsCfg(),
        mappings=MappingsCfg(),
    )
    vcs = GitLabVcs(
        config=cfg,
        gitlab_url="https://gitlab.example.com/",
        gitlab_token="glpat-FAKE-TOKEN",
        workspaces_dir=tmp_path / "workspaces",
        identity=GitIdentity(name="Virtual Dev", email="vdev@example"),
    )

    url = vcs._https_push_url_for("demo")
    assert url == (
        "https://oauth2:glpat-FAKE-TOKEN@gitlab.example.com/"
        "group/sub/demo.git"
    )


def test_clone_url_is_token_free_https_for_ssh_remote(tmp_path: Path) -> None:
    """Clone must go over HTTPS (the bot has a PAT, not an SSH key) and must
    NOT embed the token in the URL — auth comes from the credential helper, so
    origin stays token-free in .git/config."""
    cfg = AppConfig(
        repositories=[RepositoryCfg(
            key="demo",
            url="git@gitlab.example.com:group/sub/demo.git",
            default_branch="main",
        )],
        agents=AgentsCfg(),
        mappings=MappingsCfg(),
    )
    vcs = GitLabVcs(
        config=cfg,
        gitlab_url="https://gitlab.example.com/",
        gitlab_token="glpat-FAKE-TOKEN",
        workspaces_dir=tmp_path / "workspaces",
        identity=GitIdentity(name="Virtual Dev", email="vdev@example"),
    )

    url = vcs._clone_url("demo")
    assert url == "https://gitlab.example.com/group/sub/demo.git"
    assert "glpat-FAKE-TOKEN" not in url
    assert not url.startswith("git@")


def test_clone_url_passthrough_for_local_remote(tmp_path: Path) -> None:
    """A non-GitLab remote (test fixture using a local dir as upstream) is
    returned unchanged so the credential helper is never consulted for it."""
    upstream = tmp_path / "upstream"
    vcs = _vcs(tmp_path, _cfg("demo", upstream))
    assert vcs._clone_url("demo") == str(upstream)


@pytest.mark.asyncio
async def test_push_uses_https_url_with_token_not_ssh_origin(tmp_path: Path) -> None:
    """The current pipeline-attribution bug: ``git push origin <branch>``
    over an SSH remote pushes via the user's SSH key, so GitLab marks
    every pipeline as 'Created by: <user>'. Push instead via HTTPS with
    the bot's PAT embedded so attribution lands on the bot account.
    Token must NOT be persisted to .git/config (no ``--set-upstream``
    against the temp URL)."""
    # We use a local file URL for the actual fetch/clone (so workspace
    # setup works), but configure the *repo URL* in AppConfig to look
    # like a real GitLab SSH remote. ``push`` triggers HTTPS-with-PAT
    # mode based on URL shape; the underlying ``git push`` is mocked
    # below so we never actually contact the example host.
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _init_remote_repo(upstream)
    cfg = AppConfig(
        repositories=[RepositoryCfg(
            key="demo",
            url="git@gitlab.example.com:group/demo.git",
            default_branch="main",
        )],
        agents=AgentsCfg(),
        mappings=MappingsCfg(),
    )
    vcs = GitLabVcs(
        config=cfg,
        gitlab_url="https://gitlab.example.com/",
        gitlab_token="glpat-FAKE-TOKEN",
        workspaces_dir=tmp_path / "workspaces",
        identity=GitIdentity(name="Virtual Dev", email="vdev@example"),
    )
    # Manually clone from the file upstream so the workspace exists,
    # bypassing _ensure_local's URL-based clone (which would try the
    # fake gitlab.example.com host).
    workspace = tmp_path / "workspaces" / "demo"
    workspace.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "-q", str(upstream), str(workspace)],
        check=True,
    )
    vcs._verified_local_path.add("demo")
    # Override _ensure_local so it doesn't re-clone via the fake URL.
    async def _local(_repo: str) -> Path:
        return workspace
    vcs._ensure_local = _local  # type: ignore[method-assign]
    subprocess.run(
        ["git", "checkout", "-b", "ai-dev/dm-1"],
        cwd=workspace, check=True, capture_output=True,
    )
    (workspace / "x.txt").write_text("hi")
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "-c", "user.email=vdev@example", "-c", "user.name=Virtual Dev",
         "commit", "-qm", "feat: x"],
        cwd=workspace, check=True,
    )

    captured: list[list[str]] = []
    real_run_git = vcs._run_git

    async def _capture(_cwd: Path | None, *args: str) -> str:
        captured.append(list(args))
        # Don't actually exec git for `push` — we only care about args.
        if args and args[0] == "push":
            return ""
        return await real_run_git(_cwd, *args)

    vcs._run_git = _capture  # type: ignore[method-assign]
    await vcs.push("demo", "ai-dev/dm-1")

    push_calls = [a for a in captured if a and a[0] == "push"]
    assert len(push_calls) == 1, f"expected exactly one push, got {push_calls}"
    push_args = push_calls[0]
    # First arg after 'push' is the destination — must be the HTTPS-with-token URL.
    dest = push_args[1] if len(push_args) > 1 else ""
    assert dest.startswith("https://oauth2:"), (
        f"expected HTTPS-with-token push URL, got {dest!r}"
    )
    assert "@" in dest and ".git" in dest
    # The temp URL must NOT include --set-upstream (would persist token to config).
    assert "--set-upstream" not in push_args, (
        f"--set-upstream against the token URL would leak the PAT into "
        f".git/config; got args={push_args!r}"
    )

    # Workspace .git/config must remain free of the token even after push.
    cfg_text = (workspace / ".git" / "config").read_text()
    assert "glpat" not in cfg_text and "oauth2:" not in cfg_text, (
        f"PAT leaked into workspace .git/config: {cfg_text}"
    )


@pytest.mark.asyncio
async def test_commit_all_rejects_model_self_commit(tmp_path: Path) -> None:
    """If the model bypassed the prompt rule and ran ``git commit`` itself,
    HEAD lands on the local user's identity rather than the bot's. Pushing
    that anyway means MRs show up under whoever happened to be running the
    process — confusing and blocks per-repo bot-vs-human accounting. The
    runtime must refuse and force the operator/dev path to clean up rather
    than silently inheriting the wrong author.

    We simulate the rogue commit by writing a file and committing with a
    foreign identity (the bot's own commits go through ``commit_all`` which
    pins the bot identity via ``git -c user.name=...``)."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _init_remote_repo(upstream)
    vcs = _vcs(tmp_path, _cfg("demo", upstream))

    workspace = Path(await vcs.ensure_clone("demo"))
    await vcs.create_branch("demo", "ai-dev/dm-rogue", "main")

    # Model self-committed via Bash with the local user's identity.
    (workspace / "rogue.txt").write_text("hi from model\n")
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "-c", "user.email=an.volobuev@2gis.local",
                "-c", "user.name=Andrey Volobuev",
         "commit", "-qm", "[DM-1] sneaky self-commit"],
        cwd=workspace, check=True,
    )

    # Tree clean, HEAD ahead of origin, but author is NOT the bot.
    with pytest.raises(VcsRogueCommitError):
        await vcs.commit_all("demo", "noop")


@pytest.mark.asyncio
async def test_commit_all_idempotent_after_bot_commit(tmp_path: Path) -> None:
    """Counterpart to the rejection test: after ``commit_all`` itself
    creates a commit (with bot identity), a second ``commit_all`` on the
    still-clean tree must NOT mistake the bot's own prior commit for a
    rogue one. It returns the same sha so the caller's push goes through.
    Regression guard for the author-check added alongside rogue rejection."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _init_remote_repo(upstream)
    vcs = _vcs(tmp_path, _cfg("demo", upstream))

    workspace = Path(await vcs.ensure_clone("demo"))
    await vcs.create_branch("demo", "ai-dev/dm-ok", "main")
    (workspace / "ok.txt").write_text("hi from bot\n")
    sha = await vcs.commit_all("demo", "feat: add ok.txt")
    assert sha
    # No new edits, but local HEAD is still ahead of origin (we haven't
    # pushed yet). HEAD's author is the bot — must NOT raise.
    sha2 = await vcs.commit_all("demo", "noop")
    assert sha2 == sha


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
