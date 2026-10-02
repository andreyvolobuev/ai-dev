"""Port for the task tracker (Jira / Trello / GitHub Issues / ...)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from virtual_dev.domain.models.task import CreatedTask, NewTaskSpec, Task, TaskPatch


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

    # --- Write path (task intake): методы не абстрактные. Адаптерам, которым
    # создание не нужно, остаётся дефолт; фейки в тестах не перестают
    # инстанцироваться.

    async def create_task(self, spec: NewTaskSpec) -> CreatedTask:
        """Создать задачу в трекере и вернуть её ключ + URL."""
        raise NotImplementedError

    async def update_task(self, external_id: str, patch: TaskPatch) -> None:
        """Применить точечную правку к существующей задаче."""
        raise NotImplementedError

    async def find_tracker_user_by_email(self, email: str) -> str | None:
        """Логин пользователя трекера по email, или ``None``."""
        raise NotImplementedError
