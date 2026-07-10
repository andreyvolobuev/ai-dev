"""Port for VCS operations (GitLab / GitHub / Bitbucket / ...)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from virtual_dev.domain.models.merge_request import (
    ApprovalInfo,
    MergeRequest,
    PipelineJob,
    ReviewComment,
)


class VcsPort(ABC):
    """Abstraction over a VCS provider and a local checkout.

    Split is intentional: "remote" methods talk to GitLab's API, "local"
    methods operate on a working copy on disk.
    """

    # --- Local checkout ---

    @abstractmethod
    async def ensure_clone(self, repo_key: str) -> str:
        """Clone the repo if missing and return the absolute local path."""

    @abstractmethod
    async def fetch_and_checkout(self, repo_key: str, branch: str) -> None:
        """Fetch from origin and check out ``branch``."""

    @abstractmethod
    async def create_branch(self, repo_key: str, branch: str, base: str) -> None:
        """Create a new branch off ``base`` and check it out."""

    @abstractmethod
    async def checkout_existing_branch(self, repo_key: str, branch: str) -> None:
        """Fetch and check out an existing remote branch without destroying it.

        Used for Dev-agent iteration mode: the branch already has the bot's
        previous commit, we want to add a new one on top. No reset-hard to
        base, no re-creation.
        """

    @abstractmethod
    async def commit_all(self, repo_key: str, message: str) -> str:
        """Stage everything and commit. Returns the commit SHA.

        Returns an empty string if there was nothing to commit.
        """

    @abstractmethod
    async def push(self, repo_key: str, branch: str, *, force: bool = False) -> None:
        """Push ``branch`` to origin.

        ``force=True`` overwrites the remote branch — used by fresh plan
        runs, which rebuild the bot's own branch from the default branch;
        a leftover remote branch from a dead earlier run would otherwise
        reject every subsequent push as non-fast-forward."""

    @abstractmethod
    async def current_branch(self, repo_key: str) -> str:
        """Return the name of the currently checked-out branch."""

    @abstractmethod
    async def has_uncommitted_changes(self, repo_key: str) -> bool:
        """Return ``True`` iff the working tree has staged or unstaged changes."""

    async def merge_base_into_current(self, repo_key: str, base: str) -> bool:
        """Merge ``origin/<base>`` into the currently checked-out branch.

        Returns ``True`` on a clean merge (or already up-to-date),
        ``False`` if the merge conflicted (caller should give up). Default
        impl is a no-op that returns ``True`` so test fakes don't need
        boilerplate.
        """
        return True

    async def get_mr_diff(self, repo_key: str, iid: int) -> str:
        """Return the unified diff for an MR.

        Default impl returns empty string. Used by ThreadResponder so it
        can ground its decision in the actual code change instead of just
        the title + plan summary (#14 in techdebt).
        """
        return ""

    # --- Remote API ---

    @abstractmethod
    async def create_merge_request(
        self,
        repo_key: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        draft: bool = False,
    ) -> MergeRequest:
        """Open an MR (or PR) and return its representation."""

    @abstractmethod
    async def get_merge_request(self, repo_key: str, iid: int) -> MergeRequest:
        """Fetch a single MR by its in-project number."""

    @abstractmethod
    async def list_open_merge_requests(
        self, repo_key: str, author_username: str | None = None
    ) -> Sequence[MergeRequest]:
        """List open MRs, optionally filtered by author."""

    @abstractmethod
    async def list_merged_merge_requests(
        self, repo_key: str, limit: int = 500
    ) -> Sequence[MergeRequest]:
        """List most-recently merged MRs for RAG indexing.

        Ordered by ``merged_at`` descending; implementations may cap at
        ``limit`` or use page-by-page iteration internally.
        """

    @abstractmethod
    async def list_review_comments(self, repo_key: str, iid: int) -> Sequence[ReviewComment]:
        """Return review comments on the MR (both inline and general)."""

    @abstractmethod
    async def reply_to_comment(
        self, repo_key: str, iid: int, comment_id: str, body: str
    ) -> None:
        """Reply to a review comment."""

    @abstractmethod
    async def add_mr_comment(self, repo_key: str, iid: int, body: str) -> None:
        """Post a new top-level comment on the MR (not a thread reply)."""

    @abstractmethod
    async def approve_merge_request(self, repo_key: str, iid: int) -> None:
        """Add the bot's approval to an MR."""

    @abstractmethod
    async def merge(self, repo_key: str, iid: int) -> None:
        """Merge an MR. In production rarely used by the bot — humans merge."""

    async def close_merge_request(self, repo_key: str, iid: int) -> None:
        """Close an open MR without merging. Used by `/reset --with-mr`.
        Non-abstract so test fakes that never close MRs can ignore it."""
        raise NotImplementedError

    async def delete_remote_branch(self, repo_key: str, branch: str) -> None:
        """Delete a branch on the remote. Used by `/reset --with-mr`.
        Non-abstract so test fakes that never delete branches can ignore it."""
        raise NotImplementedError

    @abstractmethod
    async def get_mr_approvals(self, repo_key: str, iid: int) -> ApprovalInfo:
        """Return the current approval state for an MR."""

    @abstractmethod
    async def get_latest_pipeline_jobs(
        self, repo_key: str, iid: int, *, log_tail_lines: int = 0,
    ) -> Sequence[PipelineJob]:
        """Return jobs of the MR's latest pipeline.

        ``log_tail_lines`` for failing jobs:

        * ``> 0`` — fetch and keep the last N lines.
        * ``< 0`` — fetch the **full** log (no truncation). DevOps
          auto-fix uses this so Dev sees the real traceback, not just
          the trailing frames.
        * ``== 0`` — skip fetching the log entirely (status-only).
        """
