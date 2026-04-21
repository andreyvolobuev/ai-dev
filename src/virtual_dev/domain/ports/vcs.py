"""Port for VCS operations (GitLab / GitHub / Bitbucket / ...)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from virtual_dev.domain.models.merge_request import MergeRequest, ReviewComment


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
        """Create a new branch off ``base``."""

    @abstractmethod
    async def commit_all(self, repo_key: str, message: str) -> str:
        """Stage everything and commit. Returns the commit SHA."""

    @abstractmethod
    async def push(self, repo_key: str, branch: str) -> None:
        """Push ``branch`` to origin."""

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
    async def list_review_comments(self, repo_key: str, iid: int) -> Sequence[ReviewComment]:
        """Return review comments on the MR (both inline and general)."""

    @abstractmethod
    async def reply_to_comment(
        self, repo_key: str, iid: int, comment_id: str, body: str
    ) -> None:
        """Reply to a review comment."""

    @abstractmethod
    async def approve_merge_request(self, repo_key: str, iid: int) -> None:
        """Add the bot's approval to an MR."""

    @abstractmethod
    async def merge(self, repo_key: str, iid: int) -> None:
        """Merge an MR. In production rarely used by the bot — humans merge."""
