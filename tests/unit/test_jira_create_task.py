"""Jira-адаптер: создание задачи, активный спринт, резолв исполнителя.

Форматы sprint-поля сняты из реальной Jira Server: современный отдаёт
список словарей, старый — список строк-тострингов greenhopper. Каждый
формат встречается в проде, поэтому здесь закреплены и тот, и другой.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from virtual_dev.adapters.task_tracker.jira import (
    JiraTaskTracker,
    _parse_sprint,
    _pick_tracker_username,
    _valid_issue_type_names,
)
from virtual_dev.domain.models.task import NewTaskSpec, TaskPatch


def test_parse_sprint_modern_dicts_prefers_active() -> None:
    raw = [
        {"id": 100, "state": "closed", "name": "Sprint 41"},
        {"id": 101, "state": "active", "name": "Sprint 42"},
    ]
    assert _parse_sprint(raw) == (101, "Sprint 42")


def test_parse_sprint_legacy_greenhopper_strings() -> None:
    raw = [
        "com.atlassian.greenhopper.service.sprint.Sprint@1a2b["
        "id=567,rapidViewId=89,state=ACTIVE,name=Sprint 42,startDate=...]",
    ]
    assert _parse_sprint(raw) == (567, "Sprint 42")


def test_parse_sprint_no_active_falls_back_to_first_parsable() -> None:
    raw = [{"id": 7, "state": "future", "name": "Sprint 43"}]
    assert _parse_sprint(raw) == (7, "Sprint 43")


def test_parse_sprint_garbage_is_none() -> None:
    assert _parse_sprint(None) == (None, None)
    assert _parse_sprint([]) == (None, None)
    assert _parse_sprint(["not a sprint at all"]) == (None, None)


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
        sprint_issues: list[dict[str, Any]] | None = None,
        sprint_field_id: str = "customfield_10005",
        user_entries: list[dict[str, Any]] | None = None,
        sprint_add_raises: bool = False,
        create_raises: Exception | None = None,
        project_issue_types: list[str] | None = None,
        project_raises: bool = False,
    ) -> None:
        self._created_key = created_key
        self._sprint_issues = sprint_issues
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
        self.jql_queries: list[str] = []

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

    def jql(self, query: str, limit: int = 50) -> dict[str, Any]:
        self.jql_queries.append(query)
        return {"issues": self._sprint_issues or []}

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


def _spec(**over: Any) -> NewTaskSpec:
    base: dict[str, Any] = {
        "project": "DM",
        "issue_type": "Task",
        "summary": "Собрать жёлтые карточки по Грузии",
        "description": "Просьба из MM",
        "labels": ["dmp-sup"],
        "assignee": "ivan.ivanov",
        "add_to_active_sprint": True,
    }
    base.update(over)
    return NewTaskSpec(**base)


async def test_create_task_sends_labels_assignee_and_lands_in_active_sprint() -> None:
    client = _FakeJiraClient(
        sprint_issues=[{"fields": {"customfield_10005": [
            {"id": 101, "state": "active", "name": "Sprint 42"},
        ]}}],
    )
    created = await _tracker(client).create_task(_spec())

    assert created.key == "DM-4821"
    assert created.url == "https://jira.example/browse/DM-4821"
    assert created.sprint_name == "Sprint 42"
    assert created.warnings == []
    assert client.created_fields is not None
    assert client.created_fields["labels"] == ["dmp-sup"]
    assert client.created_fields["assignee"] == {"name": "ivan.ivanov"}
    assert client.created_fields["project"] == {"key": "DM"}
    assert client.created_fields["issuetype"] == {"name": "Task"}
    assert client.sprint_calls == [(101, ["DM-4821"])]
    assert "openSprints()" in client.jql_queries[0]


async def test_create_task_without_active_sprint_still_creates_and_warns() -> None:
    client = _FakeJiraClient(sprint_issues=[])
    created = await _tracker(client).create_task(_spec())

    assert created.key == "DM-4821"
    assert created.warnings == ["no_active_sprint"]
    assert client.sprint_calls == []


async def test_create_task_survives_sprint_api_failure() -> None:
    """Тикет уже создан — сбой спринта не должен превращаться в
    «не смогла завести задачу»."""
    client = _FakeJiraClient(
        sprint_issues=[{"fields": {"customfield_10005": [
            {"id": 101, "state": "active", "name": "Sprint 42"},
        ]}}],
        sprint_add_raises=True,
    )
    created = await _tracker(client).create_task(_spec())

    assert created.key == "DM-4821"
    assert created.sprint_name is None
    assert created.warnings == ["sprint_failed"]


async def test_create_task_without_assignee_omits_the_field() -> None:
    client = _FakeJiraClient(sprint_issues=[])
    await _tracker(client).create_task(_spec(assignee=None, add_to_active_sprint=False))

    assert client.created_fields is not None
    assert "assignee" not in client.created_fields


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


async def test_update_task_adds_to_sprint_via_agile_endpoint() -> None:
    client = _FakeJiraClient(
        sprint_issues=[{"fields": {"customfield_10005": [
            {"id": 101, "state": "active", "name": "Sprint 42"},
        ]}}],
    )
    await _tracker(client).update_task("DM-4821", TaskPatch(sprint=True))

    assert client.sprint_calls == [(101, ["DM-4821"])]
    assert client.updated == []


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
