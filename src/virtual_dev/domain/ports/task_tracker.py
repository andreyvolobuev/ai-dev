"""Port for the task tracker (Jira / Trello / GitHub Issues / ...)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from virtual_dev.domain.models.task import Task


class TaskTrackerPort(ABC):
    """Abstraction over a ticket tracker.

    Adapters must map the tracker's domain into ``Task`` and raise loudly
    on authentication / transport errors rather than swallowing them.
    """

    @abstractmethod
    async def fetch_tasks(self, jql: str, limit: int = 50) -> Sequence[Task]:
        """Return tasks matching ``jql`` (or its equivalent in the tracker)."""

    @abstractmethod
    async def get_task(self, external_id: str) -> Task:
        """Fetch a single task by its tracker-specific id (e.g. "DM-1234")."""

    @abstractmethod
    async def transition(self, external_id: str, to_status: str) -> None:
        """Move the task to ``to_status`` using the tracker's workflow."""

    @abstractmethod
    async def comment(self, external_id: str, body: str) -> None:
        """Post a comment on the task."""
