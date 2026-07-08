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
import os
import shlex
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from gitlab import Gitlab
from loguru import logger

from virtual_dev.domain.models.merge_request import (
    ApprovalInfo,
    MergeRequest,
    MRStatus,
    PipelineJob,
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


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class VcsError(RuntimeError):
    """Raised when a git or GitLab API call fails."""


class VcsRogueCommitError(VcsError):
    """Raised by ``commit_all`` when local HEAD is ahead of origin but
    its author isn't the bot — i.e. the dev model bypassed the prompt
    rule and ran ``git commit`` itself via Bash. Pushing such a commit
    would attribute the MR to whoever's local git config is set in the
    workspace (often the human operator), so we refuse and let the
    caller mark the run failed."""


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
        self._gitlab_url = gitlab_url.rstrip("/")
        self._gitlab_token = gitlab_token
        self._client = Gitlab(url=gitlab_url, private_token=gitlab_token)
        self._workspaces_dir = Path(workspaces_dir).resolve()
        self._identity = identity
        # Repos whose local_path we've already verified clean this process —
        # so ensure_clone stays idempotent after the Dev-agent starts dirtying
        # the tree with its own edits.
        self._verified_local_path: set[str] = set()
        # Per-repo lock guarding all mutating local-checkout ops (#11 in
        # techdebt). Two concurrent task runs against the same repo would
        # otherwise race on the same working tree (one's checkout / commit
        # / push trampling another).
        self._repo_locks: dict[str, asyncio.Lock] = {}
        # Separate lock family for ensure_clone: it runs both bare (pre-warm,
        # Dev-agent) and nested inside a _repo_locks holder, so it can't
        # reuse those locks without deadlocking.
        self._clone_locks: dict[str, asyncio.Lock] = {}
        # Repos whose on-disk checkout already passed the HEAD sanity check
        # this process — skip re-running `git rev-parse` on every call.
        self._clone_verified: set[str] = set()
        # Live git subprocesses (guarded by the threading lock — _git_sync
        # runs in worker threads). terminate_pending_git() kills them on
        # shutdown: a SIGTERM'd bot otherwise leaves clone/fetch children
        # running, and an orphaned clone keeps writing into workspaces/.
        self._procs_guard = threading.Lock()
        self._live_git_procs: set[subprocess.Popen[str]] = set()

    def _clone_lock(self, repo_key: str) -> asyncio.Lock:
        lock = self._clone_locks.get(repo_key)
        if lock is None:
            lock = asyncio.Lock()
            self._clone_locks[repo_key] = lock
        return lock

    def _lock(self, repo_key: str) -> asyncio.Lock:
        lock = self._repo_locks.get(repo_key)
        if lock is None:
            lock = asyncio.Lock()
            self._repo_locks[repo_key] = lock
        return lock

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

        # Serialized per repo (separate from _repo_locks, which callers such
        # as fetch_and_checkout already hold around this call — asyncio.Lock
        # is not reentrant): the startup pre-warm and a Dev-agent run may
        # request the same clone concurrently.
        async with self._clone_lock(repo_key):
            if (dest / ".git").is_dir():
                if repo_key in self._clone_verified:
                    return str(dest)
                if await self._head_resolvable(dest):
                    self._clone_verified.add(repo_key)
                    return str(dest)
                # A `.git` with no resolvable HEAD is an interrupted clone
                # (process killed mid-download). Left in place it looks
                # "ready" forever while the tree stays empty.
                logger.warning(
                    "Workspace for {} at {} is a broken half-clone — wiping and re-cloning",
                    repo_key, dest,
                )
                await asyncio.to_thread(shutil.rmtree, dest)

            dest.parent.mkdir(parents=True, exist_ok=True)
            # Clone into a hidden sibling and rename into place only when
            # complete, so `dest` either doesn't exist or is a full checkout —
            # read-only agents polling workspaces/ never see (and never poke
            # git at) a half-populated tree. The dir name embeds our pid so a
            # restarted bot never writes into (or deletes) a path an orphaned
            # clone from a killed instance is still filling.
            tmp = dest.parent / f".{dest.name}.cloning.{os.getpid()}"
            await asyncio.to_thread(self._reap_stale_clone_tmps, dest)
            if tmp.exists():
                await asyncio.to_thread(shutil.rmtree, tmp)
            clone_url = self._clone_url(repo_key)
            logger.info("Cloning {} into {}", clone_url, dest)
            # Shallow AND single-branch: the bot only ever starts from the
            # default branch's current tree — it branches off locally and
            # pushes. Other branches (e.g. its own MR branch after a restart)
            # are fetched on demand by _fetch_branch. Later fetches of the
            # default branch stay connected to the shallow boundary, so merges
            # of origin/<base> into branches created this process still find
            # their merge base.
            await self._run_git(
                None, "clone", "--depth", "1", "--single-branch",
                clone_url, str(tmp), timeout=1800,
            )
            tmp.rename(dest)
            self._clone_verified.add(repo_key)
            return str(dest)

    async def _head_resolvable(self, path: Path) -> bool:
        try:
            await self._run_git(path, "rev-parse", "--verify", "HEAD")
            return True
        except VcsError:
            return False

    def _reap_stale_clone_tmps(self, dest: Path) -> None:
        """Remove `.<repo>.cloning.<pid>` leftovers whose owner is dead.

        A SIGKILL'd bot can't clean up after itself; without this, every
        crash leaks a partial clone on disk. Dirs whose pid is still alive
        are left alone — that's a concurrent process mid-clone.
        """
        candidates = list(dest.parent.glob(f".{dest.name}.cloning.*"))
        # Pre-pid-suffix layout — no owner to check, always stale by now.
        legacy = dest.parent / f".{dest.name}.cloning"
        if legacy.exists():
            candidates.append(legacy)
        for stale in candidates:
            pid_part = stale.name.rsplit(".", 1)[-1]
            if pid_part.isdigit() and _pid_alive(int(pid_part)):
                continue
            logger.info("Removing stale clone tmp {}", stale)
            try:
                shutil.rmtree(stale)
            except OSError:
                # Racing writer (an unowned orphan still filling the dir) —
                # skip; our own tmp path is different so the clone is
                # unaffected.
                logger.warning("Could not remove stale clone tmp {}", stale)

    def terminate_pending_git(self) -> None:
        """Terminate live git subprocesses; called on bot shutdown.

        SIGTERM lets `git clone` run its own cleanup (it removes the
        destination it was filling). Anything still alive after the grace
        period is killed.
        """
        with self._procs_guard:
            procs = [p for p in self._live_git_procs if p.poll() is None]
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if procs:
            logger.info("Terminated {} in-flight git subprocess(es)", len(procs))

    async def _has_uncommitted_changes_at(self, path: Path) -> bool:
        output = await self._run_git(path, "status", "--porcelain")
        return bool(output.strip())

    async def _ensure_origin_ref(self, path: Path, branch: str) -> None:
        """Make ``origin/<branch>`` exist and point at the remote's tip.

        Clones are --single-branch, so a plain ``git fetch`` only covers the
        default branch. It runs first because it keeps the default branch's
        history connected to our shallow boundary (merges into branches
        created this process keep finding their merge base). Any other branch
        (the bot's own MR branch after a restart, a non-default base) is then
        fetched explicitly — ``--depth 1``, tip only: an old branch's history
        doesn't connect to our shallow boundary, so a full fetch would pull
        it in whole.
        """
        await self._run_git(path, "fetch", "--prune", "origin")
        try:
            await self._run_git(path, "rev-parse", "--verify", f"origin/{branch}")
        except VcsError:
            await self._run_git(
                path, "fetch", "--depth", "1", "origin",
                f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            )

    async def fetch_and_checkout(self, repo_key: str, branch: str) -> None:
        async with self._lock(repo_key):
            path = await self._ensure_local(repo_key)
            # Remote branch may not exist; fall back to local branch if so.
            try:
                await self._ensure_origin_ref(path, branch)
                await self._run_git(path, "checkout", "-B", branch, f"origin/{branch}")
            except VcsError:
                await self._run_git(path, "checkout", branch)
                return
            await self._run_git(path, "reset", "--hard", f"origin/{branch}")

    async def checkout_existing_branch(self, repo_key: str, branch: str) -> None:
        async with self._lock(repo_key):
            path = await self._ensure_local(repo_key)
            await self._ensure_origin_ref(path, branch)
            # -B ensures we move/recreate the local branch to the remote's tip
            # *without* losing the remote's commits — it's `checkout -b` if the
            # branch doesn't exist locally, `reset --hard origin/...` effectively
            # if it does. The local uncommitted state is the caller's
            # responsibility (Dev iteration already did ensure_clone with the
            # safety check).
            await self._run_git(path, "checkout", "-B", branch, f"origin/{branch}")

    async def merge_base_into_current(self, repo_key: str, base: str) -> bool:
        """Merge ``origin/<base>`` into the currently checked-out branch.

        Returns True on success (clean merge or already up-to-date), False
        when there's a conflict. On conflict we ``git merge --abort`` so
        the working tree is restored. Used by Dev iteration to keep the
        feature branch up to date with master before pushing again (#12
        in techdebt).
        """
        async with self._lock(repo_key):
            path = await self._ensure_local(repo_key)
            await self._ensure_origin_ref(path, base)
            try:
                await self._run_git_with_identity(
                    path, "merge", "--no-edit", f"origin/{base}",
                )
                return True
            except VcsError as exc:
                logger.warning(
                    "VCS: merge origin/{} into current failed: {}",
                    base, str(exc).splitlines()[0][:200],
                )
                # Best-effort abort — ignore failures (nothing to abort,
                # already-resolved, etc.) so we always return cleanly.
                try:
                    await self._run_git(path, "merge", "--abort")
                except VcsError:
                    pass
                return False

    async def create_branch(self, repo_key: str, branch: str, base: str) -> None:
        async with self._lock(repo_key):
            path = await self._ensure_local(repo_key)
            await self._ensure_origin_ref(path, base)
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
        """Stage + commit pending changes; return the SHA to push.

        Three paths:

        * Pending uncommitted changes → ``git add -A`` + commit with the
          bot's identity. Return the new HEAD sha.
        * Working tree clean, local HEAD ahead of ``origin/<branch>``,
          and HEAD authored by the bot → idempotent return of the same
          sha so the caller can still push.
        * Working tree clean, local HEAD ahead of ``origin/<branch>``,
          BUT HEAD's author isn't the bot → the model self-committed
          via Bash. Raise :class:`VcsRogueCommitError` so the caller
          aborts the run; pushing would attribute the MR to whoever's
          local git config is set in the workspace.

        Returns ``""`` only when there's truly nothing to push.
        """
        async with self._lock(repo_key):
            path = await self._ensure_local(repo_key)
            await self._run_git(path, "add", "-A")
            # Inline status check (avoid re-acquiring the lock through
            # has_uncommitted_changes).
            if (await self._run_git(path, "status", "--porcelain")).strip():
                await self._run_git_with_identity(path, "commit", "-m", message)
                return (await self._run_git(path, "rev-parse", "HEAD")).strip()
            # Clean tree — check whether the model already committed.
            branch = (
                await self._run_git(path, "rev-parse", "--abbrev-ref", "HEAD")
            ).strip()
            local_head = (await self._run_git(path, "rev-parse", "HEAD")).strip()
            try:
                remote_head = (
                    await self._run_git(path, "rev-parse", f"origin/{branch}")
                ).strip()
            except VcsError:
                # Branch doesn't exist on origin yet → any commit is "new".
                remote_head = ""
            if local_head and local_head != remote_head:
                head_author_email = (
                    await self._run_git(path, "log", "-1", "--format=%ae")
                ).strip()
                if head_author_email == self._identity.email:
                    # Our own prior commit (e.g. another commit_all call
                    # in the same flow before push). Idempotent return.
                    return local_head
                raise VcsRogueCommitError(
                    f"branch {branch!r} HEAD {local_head[:12]} authored by "
                    f"{head_author_email!r} (not bot identity "
                    f"{self._identity.email!r}); model self-committed via "
                    f"Bash despite the prompt rule. Refusing to push to "
                    f"keep the bot's commit author canonical."
                )
            return ""

    def _https_push_url_for(self, repo_key: str) -> str:
        """Build a HTTPS push URL with the bot's PAT embedded.

        The workspace's ``origin`` remote is typically the SSH form
        (``git@gitlab.host:group/project.git``) inherited from however
        the operator originally cloned. Pushing through that remote
        uses the operator's SSH key, which makes GitLab attribute the
        triggered pipeline to the operator instead of to the bot. We
        fix that by pushing to a one-off HTTPS URL that authenticates
        as the bot's PAT — pipeline attribution then follows the PAT
        owner.

        The token lands in argv (and therefore in the loguru ``git``
        debug line) but NOT in any persisted file — callers must use
        this URL one-shot, never with ``--set-upstream``.
        """
        from urllib.parse import urlparse

        host = urlparse(self._gitlab_url).hostname or ""
        if not host:
            raise VcsError(
                f"cannot derive HTTPS host from gitlab_url "
                f"{self._gitlab_url!r}"
            )
        return (
            f"https://oauth2:{self._gitlab_token}@{host}/"
            f"{self._project_path(repo_key)}.git"
        )

    async def push(self, repo_key: str, branch: str) -> None:
        """Push with limited retry.

        GitLab occasionally answers ``Internal API unreachable`` or
        similar transient errors on otherwise-healthy clusters. We've
        already invested turns into the commit by this point — a quick
        retry loop avoids throwing the whole Dev-agent run away.

        Push goes via a one-off HTTPS URL with the bot's PAT (see
        :meth:`_https_push_url_for`) instead of the workspace's SSH
        ``origin``, so GitLab attributes the pipeline to the bot
        rather than to whoever's SSH key is loaded locally.
        """
        async with self._lock(repo_key):
            path = await self._ensure_local(repo_key)
            # Real GitLab remote (git@ / https://) → push via PAT for
            # bot pipeline-attribution. Local-file remotes (test fixtures
            # using a tmp dir as "upstream") fall back to ``origin`` —
            # there's no PAT to embed and no GitLab to attribute to.
            repo_url = self._repo(repo_key).url
            if repo_url.startswith(("git@", "https://", "http://")):
                push_target = self._https_push_url_for(repo_key)
                push_args: tuple[str, ...] = (
                    "push", push_target, f"{branch}:{branch}",
                )
            else:
                push_args = ("push", "--set-upstream", "origin", branch)
            last_exc: VcsError | None = None
            for attempt in range(1, 4):
                try:
                    # No ``--set-upstream`` against the tokenised URL —
                    # that would persist the PAT into ``.git/config``.
                    # The bot's flow re-derives the URL on each push so
                    # upstream tracking isn't needed.
                    await self._run_git(path, *push_args)
                    # Keep the remote-tracking ref in sync ourselves: pushes
                    # go to a one-off URL (not the ``origin`` remote), and the
                    # --single-branch fetch refspec wouldn't map the branch
                    # anyway — without this, commit_all keeps seeing the
                    # already-pushed HEAD as "ahead of origin".
                    await self._run_git(
                        path, "update-ref",
                        f"refs/remotes/origin/{branch}", branch,
                    )
                    if attempt > 1:
                        logger.info("git push succeeded on attempt {}/3", attempt)
                    return
                except VcsError as exc:
                    message = str(exc)
                    transient = any(
                        marker in message for marker in (
                            "Internal API unreachable",
                            "could not resolve host",
                            "Connection reset",
                            "early EOF",
                            "HTTP 5",
                        )
                    )
                    if not transient or attempt == 3:
                        raise
                    last_exc = exc
                    wait = 2 * attempt
                    logger.warning(
                        "git push failed (attempt {}/3): {}. Retrying in {}s",
                        attempt, message.splitlines()[0][:160], wait,
                    )
                    await asyncio.sleep(wait)
            assert last_exc is not None
            raise last_exc

    async def current_branch(self, repo_key: str) -> str:
        # Same per-repo lock as the mutating ops: `git status`/`rev-parse`
        # refresh the index under .git/index.lock, so an unlocked read
        # racing a lock-holding commit_all fails with "index.lock exists".
        async with self._lock(repo_key):
            path = await self._ensure_local(repo_key)
            return (await self._run_git(path, "rev-parse", "--abbrev-ref", "HEAD")).strip()

    async def has_uncommitted_changes(self, repo_key: str) -> bool:
        async with self._lock(repo_key):
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
            # Self-hosted GitLab silently drops the dedicated `draft` field
            # in some versions (we observed `draft=False` on the resulting
            # MR despite sending it). The "Draft: " title prefix is the
            # canonical, version-agnostic way: GitLab parses it on create,
            # and when a human clicks "Mark as ready" GitLab strips it
            # automatically — so the prefix doesn't leak into the live
            # title once review is open.
            final_title = f"Draft: {title}" if draft else title
            payload: dict[str, Any] = {
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": final_title,
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
            # We pull through the discussions endpoint so we know which
            # discussion each note belongs to — that's what we need to
            # post threaded replies (#1 in techdebt). discussions.list()
            # is desc by default; we sort discussions by their first
            # note's created_at to get oldest-first.
            try:
                discussions = list(mr.discussions.list(all=True, iterator=True))
            except Exception:
                logger.exception(
                    "list_review_comments: discussions.list failed {}!{}",
                    repo_key, iid,
                )
                return []
            flat: list[tuple[Any, str]] = []  # (note_attrs, discussion_id)
            for disc in discussions:
                disc_id = str(getattr(disc, "id", "") or "")
                attrs = getattr(disc, "attributes", None) or {}
                for note in attrs.get("notes") or []:
                    flat.append((note, disc_id))
            # Oldest-first by note created_at (string ISO sort works).
            def _key(item: tuple[Any, str]) -> str:
                note, _ = item
                if isinstance(note, dict):
                    return str(note.get("created_at") or "")
                return str(getattr(note, "created_at", "") or "")
            flat.sort(key=_key)
            return [
                _comment_from_gitlab_dict(note, iid, disc_id)
                for note, disc_id in flat
            ]

        return await asyncio.to_thread(_run)

    async def reply_to_comment(
        self, repo_key: str, iid: int, comment_id: str, body: str
    ) -> None:
        """Reply to a comment, threaded inside its discussion when possible.

        ``comment_id`` is interpreted as a discussion id (what the
        Reviewer captures in ``ReviewComment.discussion_id``). If the
        legacy form (a note id) is passed, GitLab returns a 404 from
        ``discussions.get`` — we fall back to a top-level note so the
        reply still lands on the MR rather than disappearing.
        """
        def _run() -> None:
            project = self._client.projects.get(self._project_path(repo_key))
            mr = project.mergerequests.get(iid)
            try:
                discussion = mr.discussions.get(comment_id)
                discussion.notes.create({"body": body})
                return
            except Exception:
                logger.exception(
                    "reply_to_comment: discussion {} not found, "
                    "posting as top-level note instead", comment_id,
                )
            mr.notes.create({"body": body})

        await asyncio.to_thread(_run)

    async def add_mr_comment(self, repo_key: str, iid: int, body: str) -> None:
        def _run() -> None:
            project = self._client.projects.get(self._project_path(repo_key))
            mr = project.mergerequests.get(iid)
            mr.notes.create({"body": body})

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

    async def close_merge_request(self, repo_key: str, iid: int) -> None:
        def _run() -> None:
            project = self._client.projects.get(self._project_path(repo_key))
            mr = project.mergerequests.get(iid)
            mr.state_event = "close"
            mr.save()

        await asyncio.to_thread(_run)

    async def delete_remote_branch(self, repo_key: str, branch: str) -> None:
        def _run() -> None:
            project = self._client.projects.get(self._project_path(repo_key))
            project.branches.delete(branch)

        await asyncio.to_thread(_run)

    async def get_mr_approvals(self, repo_key: str, iid: int) -> ApprovalInfo:
        def _run() -> ApprovalInfo:
            project = self._client.projects.get(self._project_path(repo_key))
            mr = project.mergerequests.get(iid)
            try:
                approvals = mr.approvals.get()
            except Exception:
                logger.exception("get_mr_approvals: failed for {}!{}", repo_key, iid)
                return ApprovalInfo()
            raw = getattr(approvals, "approved_by", None) or []
            approved_by: list[str] = []
            for item in raw:
                # GitLab shape: [{"user": {"username": "..."}}, ...]
                if isinstance(item, dict):
                    user = item.get("user") or {}
                    name = str(user.get("username") or "")
                    if name:
                        approved_by.append(name)
            required = int(getattr(approvals, "approvals_required", 0) or 0) or 1
            return ApprovalInfo(approved_by=approved_by, required=required)

        return await asyncio.to_thread(_run)

    async def get_mr_diff(self, repo_key: str, iid: int) -> str:
        """Build a unified diff text from the MR's changes.

        We use the ``mr.changes()`` endpoint and synthesise unified-diff
        text per file so the ThreadResponder can read it without GitLab
        API knowledge. Truncated to ~50KB total to keep the LLM prompt
        manageable.
        """
        def _run() -> str:
            project = self._client.projects.get(self._project_path(repo_key))
            mr = project.mergerequests.get(iid)
            try:
                changes = mr.changes()
            except Exception:
                logger.exception("get_mr_diff: changes() failed for {}!{}", repo_key, iid)
                return ""
            files = changes.get("changes") or []
            chunks: list[str] = []
            total = 0
            limit = 50_000
            for entry in files:
                old_path = str(entry.get("old_path") or "")
                new_path = str(entry.get("new_path") or "")
                diff = str(entry.get("diff") or "")
                if not diff:
                    continue
                header = f"diff --git a/{old_path} b/{new_path}\n"
                block = header + diff
                if total + len(block) > limit:
                    chunks.append("\n[diff truncated]\n")
                    break
                chunks.append(block)
                total += len(block)
            return "\n".join(chunks)

        return await asyncio.to_thread(_run)

    async def get_latest_pipeline_jobs(
        self, repo_key: str, iid: int, *, log_tail_lines: int = 80
    ) -> list[PipelineJob]:
        def _run() -> list[PipelineJob]:
            project = self._client.projects.get(self._project_path(repo_key))
            mr = project.mergerequests.get(iid)
            try:
                pipelines = mr.pipelines.list(per_page=1, get_all=False)
            except Exception:
                logger.exception(
                    "get_latest_pipeline_jobs: list pipelines failed {}!{}", repo_key, iid,
                )
                return []
            if not pipelines:
                return []
            pipeline_raw = pipelines[0]
            try:
                pipeline = project.pipelines.get(pipeline_raw.id)
                raw_jobs = pipeline.jobs.list(all=True)
            except Exception:
                logger.exception(
                    "get_latest_pipeline_jobs: fetch jobs failed {}!{}", repo_key, iid,
                )
                return []
            out: list[PipelineJob] = []
            for job in raw_jobs:
                attrs = getattr(job, "attributes", None) or {}
                job_id = int(attrs.get("id") or getattr(job, "id", 0))
                name = str(attrs.get("name") or "")
                stage = str(attrs.get("stage") or "")
                status = str(attrs.get("status") or "")
                web_url = str(attrs.get("web_url") or "")
                tail = ""
                # log_tail_lines semantics:
                #   > 0  — fetch + keep last N lines
                #   < 0  — fetch full log (no truncation; DevOps auto-fix path)
                #   == 0 — skip log fetch entirely (Reviewer's status probe)
                if log_tail_lines != 0 and status == "failed":
                    tail = _fetch_job_log_tail(project, job_id, log_tail_lines)
                out.append(PipelineJob(
                    id=job_id, name=name, stage=stage, status=status,
                    web_url=web_url, log_excerpt=tail,
                ))
            return out

        return await asyncio.to_thread(_run)

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

    def _clone_url(self, repo_key: str) -> str:
        """HTTPS clone/fetch URL with NO embedded token.

        The bot has a GitLab PAT but no SSH key, so cloning the SSH form
        (``git@host:group/project.git`` from ``repositories.yaml``) would fail
        with ``Permission denied (publickey)``. We clone over HTTPS instead and
        let the credential helper in :meth:`_git_sync` supply the PAT — that way
        origin stays token-free (``https://host/group/project.git``) and every
        later fetch/reset/push authenticates the same way.

        Non-GitLab remotes (test fixtures that use a local dir as "upstream")
        are returned unchanged so the helper is never consulted for them.
        """
        repo_url = self._repo(repo_key).url
        if not repo_url.startswith(("git@", "https://", "http://")):
            return repo_url
        from urllib.parse import urlparse

        host = urlparse(self._gitlab_url).hostname or ""
        if not host:
            raise VcsError(
                f"cannot derive HTTPS host from gitlab_url {self._gitlab_url!r}"
            )
        return f"https://{host}/{self._project_path(repo_key)}.git"

    async def _ensure_local(self, repo_key: str) -> Path:
        path = Path(await self.ensure_clone(repo_key))
        return path

    async def _run_git(
        self, cwd: Path | None, *args: str, timeout: int = 300
    ) -> str:
        return await asyncio.to_thread(self._git_sync, cwd, list(args), False, timeout)

    async def _run_git_with_identity(self, cwd: Path | None, *args: str) -> str:
        return await asyncio.to_thread(self._git_sync, cwd, list(args), True, 300)

    def _git_sync(
        self, cwd: Path | None, args: list[str], with_identity: bool, timeout: int = 300
    ) -> str:
        cmd = ["git"]
        # Authenticate HTTPS remote ops (clone/fetch/push) as the bot's PAT
        # without persisting it in .git/config or leaking it into argv/logs:
        # a one-line credential helper reads the token from the child env
        # (GL_PAT). We first clear any inherited helper so only ours answers.
        # git consults it ONLY on an HTTP auth challenge, so this is a no-op for
        # local ops and for SSH remotes (e.g. a user's local_path checkout).
        cmd += [
            "-c", "credential.helper=",
            "-c", (
                'credential.helper=!f() { test "$1" = get && '
                'printf "username=oauth2\\npassword=%s\\n" "$GL_PAT"; }; f'
            ),
        ]
        if with_identity:
            cmd += [
                "-c", f"user.name={self._identity.name}",
                "-c", f"user.email={self._identity.email}",
            ]
        cmd += args
        logger.debug("git {}", " ".join(shlex.quote(a) for a in args))
        # GL_PAT feeds the credential helper above; GIT_TERMINAL_PROMPT=0 makes
        # git fail fast instead of blocking on a username/password prompt when
        # auth is missing or wrong.
        env = {
            **os.environ,
            "GL_PAT": self._gitlab_token,
            "GIT_TERMINAL_PROMPT": "0",
        }
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd) if cwd else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        except FileNotFoundError as exc:
            raise VcsError("git CLI not found on PATH") from exc
        # Register so terminate_pending_git() can reach in-flight children on
        # shutdown — otherwise a SIGTERM'd bot leaves clones running orphaned.
        with self._procs_guard:
            self._live_git_procs.add(proc)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.communicate()
            raise VcsError(f"git {args[0]} timed out") from exc
        finally:
            with self._procs_guard:
                self._live_git_procs.discard(proc)

        if proc.returncode != 0:
            stderr = (stderr or "").strip()
            raise VcsError(
                f"git {args[0]} failed (exit={proc.returncode}): {stderr or '(no stderr)'}"
            )
        return stdout or ""


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
        system=bool(getattr(note, "system", False)),
    )


def _comment_from_gitlab_dict(
    note: Any, iid: int, discussion_id: str,
) -> ReviewComment:
    """Build a ReviewComment from a discussions endpoint note (dict form)."""
    if isinstance(note, dict):
        author = note.get("author") or {}
        author_username = str(author.get("username") or "") if isinstance(author, dict) else ""
        from datetime import datetime as _dt
        created_at_raw = note.get("created_at")
        created_at = None
        if isinstance(created_at_raw, str):
            try:
                created_at = _dt.fromisoformat(created_at_raw.replace("Z", "+00:00"))
            except ValueError:
                created_at = None
        return ReviewComment(
            id=str(note.get("id") or ""),
            mr_id=str(iid),
            author_username=author_username,
            body=str(note.get("body") or ""),
            resolved=bool(note.get("resolvable") and note.get("resolved")),
            system=bool(note.get("system")),
            discussion_id=discussion_id or None,
            created_at=created_at,
        )
    # Object-form fallback (older python-gitlab).
    base = _comment_from_gitlab(note, iid)
    base.discussion_id = discussion_id or None
    return base


def _fetch_job_log_tail(project: Any, job_id: int, tail_lines: int) -> str:
    """Pull a failing job's trace from GitLab.

    ``tail_lines <= 0`` returns the full log (used by DevOps auto-fix —
    Dev needs the whole picture, not just the last 80 lines). Positive
    values keep only the tail so MM messages don't explode.
    """
    try:
        job = project.jobs.get(job_id)
        raw = job.trace()
    except Exception:
        logger.exception("fetch_job_log_tail: job={} failed", job_id)
        return ""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw)
    if tail_lines <= 0:
        return text
    lines = text.splitlines()
    if len(lines) > tail_lines:
        lines = lines[-tail_lines:]
    return "\n".join(lines)
