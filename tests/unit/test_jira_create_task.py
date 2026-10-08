"""Jira adapter: task creation, sprint resolved by name, assignee lookup."""

from __future__ import annotations

from typing import Any

import pytest
import requests

from virtual_dev.adapters.task_tracker.jira import (
    JiraTaskTracker,
    _pick_tracker_username,
    _valid_issue_type_names,
)
from virtual_dev.domain.models.task import NewTaskSpec, TaskPatch


def test_pick_tracker_username_exact_email_match() -> None:
    entries = [
        {"name": "other.person", "emailAddress": "other@2gis.ru"},
        {"name": "ivan.ivanov", "emailAddress": "Ivan.Ivanov@2GIS.ru"},
    ]
    assert _pick_tracker_username(entries, "ivan.ivanov@2gis.ru") == "ivan.ivanov"


def test_pick_tracker_username_single_hit_without_email_is_accepted() -> None:
    """Jira Server часто скрывает email в выдаче поиска. Единственный
    результат по точному email-запросу — это он и есть."""
    entries = [{"name": "ivan.ivanov"}]
    assert _pick_tracker_username(entries, "ivan.ivanov@2gis.ru") == "ivan.ivanov"


def test_pick_tracker_username_ambiguous_without_email_is_none() -> None:
    """Два результата и ни одного email — назначить наугад нельзя."""
    entries = [{"name": "ivan.ivanov"}, {"name": "ivan.ivanoff"}]
    assert _pick_tracker_username(entries, "ivan.ivanov@2gis.ru") is None


class _FakeJiraClient:
    """Минимальный дубль atlassian-python-api клиента."""

    def __init__(
        self,
        *,
        created_key: str = "DM-4821",
        boards: list[dict[str, Any]] | None = None,
        sprints_by_board: dict[int, list[dict[str, Any]]] | None = None,
        page_size: int = 50,
        sprint_field_id: str = "customfield_10005",
        user_entries: list[dict[str, Any]] | None = None,
        sprint_add_raises: bool = False,
        create_raises: Exception | None = None,
        project_issue_types: list[str] | None = None,
        project_raises: bool = False,
    ) -> None:
        self._created_key = created_key
        self._boards = boards if boards is not None else [{"id": 803}]
        self._sprints_by_board = sprints_by_board or {}
        self._page_size = page_size
        self._sprint_field_id = sprint_field_id
        self._user_entries = user_entries or []
        self._sprint_add_raises = sprint_add_raises
        self._create_raises = create_raises
        self._project_issue_types = (
            project_issue_types
            if project_issue_types is not None
            else ["Усовершенствование", "Задача", "Ошибка"]
        )
        self._project_raises = project_raises
        self.project_calls: list[str] = []
        self.created_fields: dict[str, Any] | None = None
        self.sprint_calls: list[tuple[int, list[str]]] = []
        self.updated: list[tuple[str, dict[str, Any]]] = []
        self.board_calls: list[str | None] = []
        self.sprint_list_calls: list[int] = []

    def create_issue(self, fields: dict[str, Any]) -> dict[str, Any]:
        self.created_fields = fields
        if self._create_raises is not None:
            raise self._create_raises
        return {"key": self._created_key}

    def get_project(self, key: str) -> dict[str, Any]:
        self.project_calls.append(key)
        if self._project_raises:
            raise RuntimeError("project endpoint down")
        return {
            "key": key,
            "issueTypes": [{"name": name} for name in self._project_issue_types],
        }

    def get_all_fields(self) -> list[dict[str, Any]]:
        return [
            {"id": "customfield_10001", "name": "Story Points"},
            {"id": self._sprint_field_id, "name": "Sprint"},
        ]

    def _page(self, items: list[dict[str, Any]], start: int, limit: int) -> dict[str, Any]:
        size = min(limit, self._page_size)
        chunk = items[start:start + size]
        return {"values": chunk, "isLast": start + size >= len(items)}

    def get_all_agile_boards(
        self, project_key: str | None = None, start: int = 0, limit: int = 50, **_: Any,
    ) -> dict[str, Any]:
        self.board_calls.append(project_key)
        return self._page(self._boards, start, limit)

    def get_all_sprints_from_board(
        self, board_id: int, state: str | None = None, start: int = 0, limit: int = 50,
    ) -> dict[str, Any]:
        self.sprint_list_calls.append(board_id)
        return self._page(self._sprints_by_board.get(board_id, []), start, limit)

    def add_issues_to_sprint(self, sprint_id: int, issues: list[str]) -> None:
        if self._sprint_add_raises:
            raise RuntimeError("sprint API down")
        self.sprint_calls.append((sprint_id, issues))

    def update_issue_field(self, key: str, fields: dict[str, Any]) -> None:
        self.updated.append((key, fields))

    def issue_field_value(self, key: str, field: str) -> Any:
        return ["dmp-sup"]

    def user_find_by_user_string(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self._user_entries


def _tracker(client: _FakeJiraClient) -> JiraTaskTracker:
    tracker = JiraTaskTracker(url="https://jira.example/", token="t")
    tracker._client = client  # type: ignore[assignment]
    return tracker


_QUEUE = "DM. Распределительная пещера"


def _queue_sprints() -> dict[int, list[dict[str, Any]]]:
    return {
        803: [
            {"id": 3000, "name": "DM. Спринт 41", "state": "closed"},
            {"id": 3001, "name": "DM. Спринт 42", "state": "active"},
            {"id": 2999, "name": _QUEUE, "state": "closed"},
            {"id": 3024, "name": _QUEUE, "state": "future"},
        ],
    }


def _spec(**over: Any) -> NewTaskSpec:
    base: dict[str, Any] = {
        "project": "DM",
        "issue_type": "Task",
        "summary": "[SUPPORT] Собрать жёлтые карточки по Грузии",
        "description": "Просьба из MM",
        "labels": ["dmp-sup"],
        "assignee": None,
        "components": ["DM-Common"],
        "sprint_name": _QUEUE,
        "customer": "@ivanov",
    }
    base.update(over)
    return NewTaskSpec(**base)


def _tracker_with_customer(client: _FakeJiraClient) -> JiraTaskTracker:
    tracker = JiraTaskTracker(
        url="https://jira.example/", token="t", customer_field_id="customfield_32545",
    )
    tracker._client = client  # type: ignore[assignment]
    return tracker


async def test_create_task_sends_components_labels_and_customer() -> None:
    client = _FakeJiraClient(sprints_by_board=_queue_sprints())
    await _tracker_with_customer(client).create_task(_spec())

    fields = client.created_fields
    assert fields is not None
    assert fields["components"] == [{"name": "DM-Common"}]
    assert fields["customfield_32545"] == "@ivanov"
    assert fields["labels"] == ["dmp-sup"]
    assert fields["project"] == {"key": "DM"}
    assert fields["issuetype"] == {"name": "Task"}
    assert "assignee" not in fields


async def test_create_task_without_customer_field_id_does_not_set_it() -> None:
    client = _FakeJiraClient(sprints_by_board=_queue_sprints())
    await _tracker(client).create_task(_spec())

    assert client.created_fields is not None
    assert not [k for k in client.created_fields if k.startswith("customfield_")]


async def test_create_task_omits_empty_customer_and_components() -> None:
    client = _FakeJiraClient(sprints_by_board=_queue_sprints())
    tracker = _tracker_with_customer(client)
    await tracker.create_task(_spec(customer="", components=[]))

    assert client.created_fields is not None
    assert "customfield_32545" not in client.created_fields
    assert "components" not in client.created_fields


async def test_create_task_sends_assignee_when_one_is_named() -> None:
    client = _FakeJiraClient(sprints_by_board=_queue_sprints())
    await _tracker(client).create_task(_spec(assignee="petr.petrov"))

    assert client.created_fields is not None
    assert client.created_fields["assignee"] == {"name": "petr.petrov"}


async def test_create_task_resolves_sprint_by_name_and_adds_the_ticket() -> None:
    client = _FakeJiraClient(sprints_by_board=_queue_sprints())
    created = await _tracker(client).create_task(_spec())

    assert created.key == "DM-4821"
    assert created.url == "https://jira.example/browse/DM-4821"
    assert created.sprint_name == _QUEUE
    assert created.warnings == []
    # The closed sprint with the same name must not win.
    assert client.sprint_calls == [(3024, ["DM-4821"])]
    assert client.board_calls == ["DM"]


async def test_create_task_finds_the_sprint_on_a_later_board_and_page() -> None:
    client = _FakeJiraClient(
        boards=[{"id": 803}, {"id": 1804}],
        sprints_by_board={
            803: [{"id": i, "name": f"Other {i}", "state": "closed"} for i in range(5)],
            1804: [
                *[{"id": 100 + i, "name": f"Other {i}", "state": "future"} for i in range(4)],
                {"id": 3024, "name": _QUEUE, "state": "future"},
            ],
        },
        page_size=2,
    )
    created = await _tracker(client).create_task(_spec())

    assert created.warnings == []
    assert client.sprint_calls == [(3024, ["DM-4821"])]


async def test_sprint_id_is_cached_per_process() -> None:
    client = _FakeJiraClient(sprints_by_board=_queue_sprints())
    tracker = _tracker(client)
    await tracker.create_task(_spec())
    await tracker.create_task(_spec())

    assert client.sprint_calls == [(3024, ["DM-4821"]), (3024, ["DM-4821"])]
    assert client.board_calls == ["DM"]
    assert client.sprint_list_calls == [803]


async def test_unresolved_sprint_is_not_cached() -> None:
    client = _FakeJiraClient(sprints_by_board={803: []})
    tracker = _tracker(client)
    await tracker.create_task(_spec())
    client._sprints_by_board = _queue_sprints()
    created = await tracker.create_task(_spec())

    assert created.warnings == []
    assert client.sprint_calls == [(3024, ["DM-4821"])]


async def test_create_task_with_unknown_sprint_still_creates_and_warns() -> None:
    client = _FakeJiraClient(sprints_by_board={803: [{"id": 1, "name": "x", "state": "future"}]})
    created = await _tracker(client).create_task(_spec())

    assert created.key == "DM-4821"
    assert created.sprint_name is None
    assert created.warnings == ["sprint_not_found"]
    assert client.sprint_calls == []


async def test_create_task_with_only_a_closed_sprint_of_that_name_warns() -> None:
    client = _FakeJiraClient(
        sprints_by_board={803: [{"id": 2999, "name": _QUEUE, "state": "closed"}]},
    )
    created = await _tracker(client).create_task(_spec())

    assert created.warnings == ["sprint_not_found"]


async def test_create_task_with_empty_sprint_name_skips_sprints_entirely() -> None:
    client = _FakeJiraClient(sprints_by_board=_queue_sprints())
    created = await _tracker(client).create_task(_spec(sprint_name=None))

    assert created.warnings == []
    assert created.sprint_name is None
    assert client.board_calls == []
    assert client.sprint_calls == []


async def test_create_task_survives_sprint_api_failure() -> None:
    """The ticket already exists - a sprint failure must not turn into
    "could not file the task"."""
    client = _FakeJiraClient(sprints_by_board=_queue_sprints(), sprint_add_raises=True)
    created = await _tracker(client).create_task(_spec())

    assert created.key == "DM-4821"
    assert created.sprint_name is None
    assert created.warnings == ["sprint_failed"]


async def test_update_task_sets_summary_and_assignee() -> None:
    client = _FakeJiraClient()
    await _tracker(client).update_task(
        "DM-4821", TaskPatch(summary="Новое имя", assignee="petr.petrov"),
    )

    assert client.updated == [(
        "DM-4821",
        {"summary": "Новое имя", "assignee": {"name": "petr.petrov"}},
    )]


async def test_update_task_removes_from_sprint() -> None:
    client = _FakeJiraClient()
    await _tracker(client).update_task("DM-4821", TaskPatch(sprint=False))

    assert client.updated == [("DM-4821", {"customfield_10005": None})]


async def test_update_task_returns_to_the_named_sprint_via_agile_endpoint() -> None:
    client = _FakeJiraClient(sprints_by_board=_queue_sprints())
    await _tracker(client).update_task(
        "DM-4821", TaskPatch(sprint=True, sprint_name=_QUEUE),
    )

    assert client.sprint_calls == [(3024, ["DM-4821"])]
    assert client.updated == []
    assert client.board_calls == ["DM"]


async def test_update_task_sprint_true_without_a_name_does_nothing() -> None:
    client = _FakeJiraClient(sprints_by_board=_queue_sprints())
    await _tracker(client).update_task("DM-4821", TaskPatch(sprint=True))

    assert client.sprint_calls == []


async def test_update_task_empty_patch_touches_nothing() -> None:
    client = _FakeJiraClient()
    await _tracker(client).update_task("DM-4821", TaskPatch())

    assert client.updated == []
    assert client.sprint_calls == []


async def test_find_tracker_user_by_email() -> None:
    client = _FakeJiraClient(user_entries=[
        {"name": "ivan.ivanov", "emailAddress": "ivan.ivanov@2gis.ru"},
    ])
    found = await _tracker(client).find_tracker_user_by_email("ivan.ivanov@2gis.ru")
    assert found == "ivan.ivanov"


async def test_find_tracker_user_by_email_not_found() -> None:
    client = _FakeJiraClient(user_entries=[])
    assert await _tracker(client).find_tracker_user_by_email("nobody@2gis.ru") is None


@pytest.mark.parametrize("email", ["", "   "])
async def test_find_tracker_user_by_email_ignores_blank(email: str) -> None:
    client = _FakeJiraClient(user_entries=[{"name": "x", "emailAddress": "x@2gis.ru"}])
    assert await _tracker(client).find_tracker_user_by_email(email) is None


def test_valid_issue_type_names_reads_them_from_the_project() -> None:
    client = _FakeJiraClient(project_issue_types=["Задача", "Ошибка"])
    assert _valid_issue_type_names(client, "DM") == ["Задача", "Ошибка"]


def test_valid_issue_type_names_is_silent_when_the_lookup_fails() -> None:
    """Diagnostics on the failure path must never replace the original
    Jira error."""
    client = _FakeJiraClient(project_raises=True)
    assert _valid_issue_type_names(client, "DM") == []


async def test_create_task_looks_up_valid_types_and_reraises_the_original() -> None:
    """Jira answers "The issue type selected is invalid" without naming the
    types it would accept — exactly how this feature failed in production,
    where project DM names its types in Russian. The adapter must pull that
    list into the log yet re-raise the ORIGINAL exception: the runner picks
    the human reply from its HTTP status.
    """
    response = requests.Response()
    response.status_code = 400
    original = requests.HTTPError("The issue type selected is invalid.", response=response)
    client = _FakeJiraClient(create_raises=original)

    with pytest.raises(requests.HTTPError) as caught:
        await _tracker(client).create_task(_spec())

    assert caught.value is original
    assert caught.value.response is response
    assert client.project_calls == ["DM"]
