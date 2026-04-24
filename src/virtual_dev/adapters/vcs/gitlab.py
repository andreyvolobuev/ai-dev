"""GitLab-backed :class:`VcsPort` implementation.

* Local git operations go through ``subprocess`` (wrapped in
  ``asyncio.to_thread``). We don't use GitPython because it tends to hold
  git state open in ways that conflict with our "fire-and-forget clone /
  fetch" model, and plain git is trivially reliable.
* Remote API operations go through ``python-gitlab``.

Workspace layout:

    ``<workspaces_dir>/<repo_key>/``  is the bot's dedicated checkout.
    It is separate from the user's hand-edited working copy so the two
    do not step on each other.

Commit author identity is provided via ``GitIdentity`` and passed per-call
with ``-c user.name=... -c user.email=...`` — no global ``git config``
mutation, no surprise when the user runs git in the same shell.
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from gitlab import Gitlab
from loguru import logger

from virtual_dev.domain.models.merge_request import (
    MergeRequest,
    MRStatus,
    PipelineStatus,
    ReviewComment,
)
from virtual_dev.domain.ports.vcs import VcsPort
from virtual_dev.infrastructure.config import AppConfig, RepositoryCfg


@dataclass
class GitIdentity:
    """Author / committer identity the bot stamps on its commits."""

    name: str
    email: str


class VcsError(RuntimeError):
    """Raised when a git or GitLab API call fails."""


class GitLabVcs(VcsPort):
    def __init__(
        self,
        *,
        config: AppConfig,
        gitlab_url: str,
        gitlab_token: str,
        workspaces_dir: str | Path,
        identity: GitIdentity,
    ) -> None:
        if not gitlab_url or not gitlab_token:
            raise ValueError("GitLab URL and token must be provided")
        self._config = config
        self._client = Gitlab(url=gitlab_url, private_token=gitlab_token)
        self._workspaces_dir = Path(workspaces_dir).resolve()
        self._identity = identity
        # Repos whose local_path we've already verified clean this process —
        # so ensure_clone stays idempotent after the Dev-agent starts dirtying
        # the tree with its own edits.
        self._verified_local_path: set[str] = set()

    # --- Local checkout ---

    async def ensure_clone(self, repo_key: str) -> str:
        repo_cfg = self._repo(repo_key)
        dest = self._workspace_path(repo_key)

        # If repositories.yaml pins a local_path, reuse the user's existing
        # checkout instead of re-cloning. On the FIRST call this process:
        # refuse if the tree is dirty — the Dev-agent will do reset --hard /
        # branch switches that would clobber uncommitted work. Subsequent
        # calls skip the check, because the Dev-agent itself dirties the
        # tree as it edits files before commit_all.
        if repo_cfg.local_path:
            if not (dest / ".git").is_dir():
                raise VcsError(
                    f"local_path for {repo_key!r} ({dest}) is not a git repo — "
                    f"fix repositories.yaml or clone it yourself"
                )
            if repo_key not in self._verified_local_path:
                if await self._has_uncommitted_changes_at(dest):
                    raise VcsError(
                        f"local_path for {repo_key!r} ({dest}) has uncommitted "
                        f"changes; stash or commit before running the Dev-agent "
                        f"(it would reset --hard / switch branches and wipe them)"
                    )
                self._verified_local_path.add(repo_key)
            return str(dest)

        if (dest / ".git").is_dir():
            return str(dest)

        dest.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Cloning {} into {}", repo_cfg.url, dest)
        await self._run_git(None, "clone", repo_cfg.url, str(dest))
        return str(dest)

    async def _has_uncommitted_changes_at(self, path: Path) -> bool:
        output = await self._run_git(path, "status", "--porcelain")
        return bool(output.strip())

    async def fetch_and_checkout(self, repo_key: str, branch: str) -> None:
        path = await self._ensure_local(repo_key)
        await self._run_git(path, "fetch", "--prune", "origin")
        # Remote branch may not exist; fall back to local branch if so.
        try:
            await self._run_git(path, "checkout", "-B", branch, f"origin/{branch}")
        except VcsError:
            await self._run_git(path, "checkout", branch)
        await self._run_git(path, "reset", "--hard", f"origin/{branch}")

    async def create_branch(self, repo_key: str, branch: str, base: str) -> None:
        path = await self._ensure_local(repo_key)
        await self._run_git(path, "fetch", "--prune", "origin")
        # Refresh base before branching off.
        try:
            await self._run_git(path, "checkout", "-B", base, f"origin/{base}")
        except VcsError:
            await self._run_git(path, "checkout", base)
        # Delete any stale local branch with the same name so we start fresh.
        try:
            await self._run_git(path, "branch", "-D", branch)
        except VcsError:
            pass
        await self._run_git(path, "checkout", "-b", branch)

    async def commit_all(self, repo_key: str, message: str) -> str:
        path = await self._ensure_local(repo_key)
        await self._run_git(path, "add", "-A")
        # Nothing to commit? Return an empty sha.
        if not await self.has_uncommitted_changes(repo_key):
            return ""
        await self._run_git_with_identity(path, "commit", "-m", message)
        sha = (await self._run_git(path, "rev-parse", "HEAD")).strip()
        return sha

    async def push(self, repo_key: str, branch: str) -> None:
        path = await self._ensure_local(repo_key)
        await self._run_git(path, "push", "--set-upstream", "origin", branch)

    async def current_branch(self, repo_key: str) -> str:
        path = await self._ensure_local(repo_key)
        return (await self._run_git(path, "rev-parse", "--abbrev-ref", "HEAD")).strip()

    async def has_uncommitted_changes(self, repo_key: str) -> bool:
        path = await self._ensure_local(repo_key)
        output = await self._run_git(path, "status", "--porcelain")
        return bool(output.strip())

    # --- Remote API ---

    async def create_merge_request(
        self,
        repo_key: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        draft: bool = False,
    ) -> MergeRequest:
        def _run() -> MergeRequest:
            project = self._client.projects.get(self._project_path(repo_key))
            payload: dict[str, Any] = {
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": ("Draft: " + title) if draft else title,
                "description": description,
                "remove_source_branch": True,
            }
            mr = project.mergerequests.create(payload)
            return _mr_from_gitlab(mr)

        return await asyncio.to_thread(_run)

    async def get_merge_request(self, repo_key: str, iid: int) -> MergeRequest:
        def _run() -> MergeRequest:
            project = self._client.projects.get(self._project_path(repo_key))
            return _mr_from_gitlab(project.mergerequests.get(iid))

        return await asyncio.to_thread(_run)

    async def list_open_merge_requests(
        self, repo_key: str, author_username: str | None = None
    ) -> list[MergeRequest]:
        def _run() -> list[MergeRequest]:
            project = self._client.projects.get(self._project_path(repo_key))
            kwargs: dict[str, Any] = {"state": "opened", "all": True}
            if author_username:
                kwargs["author_username"] = author_username
            return [_mr_from_gitlab(mr) for mr in project.mergerequests.list(**kwargs)]

        return await asyncio.to_thread(_run)

    async def list_merged_merge_requests(
        self, repo_key: str, limit: int = 500
    ) -> list[MergeRequest]:
        def _run() -> list[MergeRequest]:
            project = self._client.projects.get(self._project_path(repo_key))
            # GitLab orders by created_at by default; switch to updated_at
            # descending which is the closest proxy for "recent merges".
            raw = project.mergerequests.list(
                state="merged",
                order_by="updated_at",
                sort="desc",
                per_page=min(limit, 100),
                iterator=True,
            )
            out: list[MergeRequest] = []
            for mr in raw:
                out.append(_mr_from_gitlab(mr))
                if len(out) >= limit:
                    break
            return out

        return await asyncio.to_thread(_run)

    async def list_review_comments(self, repo_key: str, iid: int) -> list[ReviewComment]:
        def _run() -> list[ReviewComment]:
            project = self._client.projects.get(self._project_path(repo_key))
            mr = project.mergerequests.get(iid)
            return [_comment_from_gitlab(n, iid) for n in mr.notes.list(all=True)]

        return await asyncio.to_thread(_run)

    async def reply_to_comment(
        self, repo_key: str, iid: int, comment_id: str, body: str
    ) -> None:
        def _run() -> None:
            project = self._client.projects.get(self._project_path(repo_key))
            mr = project.mergerequests.get(iid)
            discussion = mr.discussions.get(comment_id)
            discussion.notes.create({"body": body})

        await asyncio.to_thread(_run)

    async def approve_merge_request(self, repo_key: str, iid: int) -> None:
        def _run() -> None:
            project = self._client.projects.get(self._project_path(repo_key))
            mr = project.mergerequests.get(iid)
            mr.approve()

        await asyncio.to_thread(_run)

    async def merge(self, repo_key: str, iid: int) -> None:
        def _run() -> None:
            project = self._client.projects.get(self._project_path(repo_key))
            mr = project.mergerequests.get(iid)
            mr.merge()

        await asyncio.to_thread(_run)

    # --- helpers ---

    def _repo(self, repo_key: str) -> RepositoryCfg:
        repo = self._config.get_repository(repo_key)
        if repo is None:
            raise VcsError(f"Unknown repository: {repo_key!r}")
        return repo

    def _workspace_path(self, repo_key: str) -> Path:
        """Resolve the local checkout for a repo.

        Honours ``local_path`` from ``repositories.yaml`` (same as the
        Researcher) so the Dev-agent reuses the user's existing clone
        instead of fetching a second copy into ``workspaces/``.
        """
        repo_cfg = self._repo(repo_key)
        if repo_cfg.local_path:
            return Path(repo_cfg.local_path).expanduser().resolve()
        return self._workspaces_dir / repo_key

    def _project_path(self, repo_key: str) -> str:
        """Derive the GitLab project "namespace/name" from the repo URL."""
        url = self._repo(repo_key).url
        # git@host:group/sub/project.git  →  group/sub/project
        if ":" in url and "@" in url:
            path = url.split(":", 1)[1]
        else:
            from urllib.parse import urlparse

            path = urlparse(url).path.lstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return path

    async def _ensure_local(self, repo_key: str) -> Path:
        path = Path(await self.ensure_clone(repo_key))
        return path

    async def _run_git(self, cwd: Path | None, *args: str) -> str:
        return await asyncio.to_thread(self._git_sync, cwd, list(args), False)

    async def _run_git_with_identity(self, cwd: Path | None, *args: str) -> str:
        return await asyncio.to_thread(self._git_sync, cwd, list(args), True)

    def _git_sync(self, cwd: Path | None, args: list[str], with_identity: bool) -> str:
        cmd = ["git"]
        if with_identity:
            cmd += [
                "-c", f"user.name={self._identity.name}",
                "-c", f"user.email={self._identity.email}",
            ]
        cmd += args
        logger.debug("git {}", " ".join(shlex.quote(a) for a in args))
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd) if cwd else None,
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except FileNotFoundError as exc:
            raise VcsError("git CLI not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise VcsError(f"git {args[0]} timed out") from exc

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            raise VcsError(
                f"git {args[0]} failed (exit={proc.returncode}): {stderr or '(no stderr)'}"
            )
        return proc.stdout or ""


# --- helpers: gitlab object → domain model ---


def _mr_from_gitlab(mr: Any) -> MergeRequest:
    state = str(getattr(mr, "state", "") or "opened").lower()
    status = {
        "opened": MRStatus.OPEN,
        "merged": MRStatus.MERGED,
        "closed": MRStatus.CLOSED,
        "locked": MRStatus.CLOSED,
    }.get(state, MRStatus.OPEN)
    if getattr(mr, "draft", False) or getattr(mr, "work_in_progress", False):
        status = MRStatus.DRAFT

    pipeline_status = PipelineStatus.UNKNOWN
    pipeline_url: str | None = None
    pipeline = getattr(mr, "pipeline", None)
    if isinstance(pipeline, dict):
        raw = str(pipeline.get("status") or "").lower()
        mapping = {
            "pending": PipelineStatus.PENDING,
            "running": PipelineStatus.RUNNING,
            "success": PipelineStatus.SUCCESS,
            "failed": PipelineStatus.FAILED,
            "canceled": PipelineStatus.CANCELLED,
            "cancelled": PipelineStatus.CANCELLED,
        }
        pipeline_status = mapping.get(raw, PipelineStatus.UNKNOWN)
        pipeline_url = pipeline.get("web_url")

    return MergeRequest(
        id=str(getattr(mr, "id", "")),
        iid=int(getattr(mr, "iid", 0)),
        project_id=str(getattr(mr, "project_id", "")),
        title=str(getattr(mr, "title", "")),
        description=str(getattr(mr, "description", "") or ""),
        source_branch=str(getattr(mr, "source_branch", "")),
        target_branch=str(getattr(mr, "target_branch", "")),
        author_username=str(cast(dict[str, Any], getattr(mr, "author", {}) or {}).get("username", "")),
        web_url=str(getattr(mr, "web_url", "")),
        status=status,
        pipeline_status=pipeline_status,
        pipeline_url=pipeline_url,
    )


def _comment_from_gitlab(note: Any, iid: int) -> ReviewComment:
    return ReviewComment(
        id=str(getattr(note, "id", "")),
        mr_id=str(iid),
        author_username=str(cast(dict[str, Any], getattr(note, "author", {}) or {}).get("username", "")),
        body=str(getattr(note, "body", "")),
        resolved=bool(getattr(note, "resolvable", False) and getattr(note, "resolved", False)),
    )
