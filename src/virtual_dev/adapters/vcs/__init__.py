"""VCS adapters."""

from virtual_dev.adapters.vcs.gitlab import GitIdentity, GitLabVcs, VcsError

__all__ = ["GitIdentity", "GitLabVcs", "VcsError"]
