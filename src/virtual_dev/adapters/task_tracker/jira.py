"""Jira adapter (self-hosted) via ``atlassian-python-api``.

Synchronous under the hood — we wrap calls in ``asyncio.to_thread`` so the
orchestrator stays fully async.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from atlassian import Jira
from loguru import logger

from virtual_dev.domain.models.task import Task, TaskLink, TaskPriority, TaskStatus
from virtual_dev.domain.ports.task_tracker import TaskTrackerPort

_PRIORITY_MAP: dict[str, TaskPriority] = {
    "lowest": TaskPriority.LOW,
    "low": TaskPriority.LOW,
    "medium": TaskPriority.MEDIUM,
    "high": TaskPriority.HIGH,
    "highest": TaskPriority.CRITICAL,
    "critical": TaskPriority.CRITICAL,
    "blocker": TaskPriority.CRITICAL,
}


def _parse_jira_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    # Jira returns e.g. "2025-03-05T10:20:30.000+0300".
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        # Fall back: strip timezone suffix without colon (2025-03-05T10:20:30.000+0300).
        try:
            head, tz = raw[:-5], raw[-5:]
            return datetime.fromisoformat(f"{head}{tz[:3]}:{tz[3:]}")
        except ValueError:
            logger.warning("could not parse jira datetime: {!r}", raw)
            return None


class JiraTaskTracker(TaskTrackerPort):
    """``TaskTrackerPort`` backed by Jira (self-hosted)."""

    def __init__(
        self,
        *,
        url: str,
        token: str,
        user: str = "",          # kept for backward-compat; not used for PAT auth
        browse_base_url: str | None = None,
    ) -> None:
        if not url or not token:
            raise ValueError("Jira URL and token must be provided")
        # PAT (Personal Access Token) authentication — sends
        # ``Authorization: Bearer <token>`` which is what Jira Server/DC
        # expects. The old ``username=user, password=token`` form sent Basic
        # Auth which PATs don't support in most Jira Server configurations.
        self._client = Jira(url=url, token=token, cloud=False)
        # For building web URLs to tickets.
        self._browse_base_url = (browse_base_url or url).rstrip("/")

    async def fetch_tasks(self, jql: str, limit: int = 50) -> Sequence[Task]:
        def _fetch() -> list[dict[str, Any]]:
            result = self._client.jql(jql, limit=limit)
            if not isinstance(result, dict):
                raise RuntimeError(f"Unexpected Jira response: {type(result).__name__}")
            return cast(list[dict[str, Any]], result.get("issues", []))

        issues = await asyncio.to_thread(_fetch)
        return [self._issue_to_task(issue) for issue in issues]

    async def get_task(self, external_id: str) -> Task:
        def _fetch() -> dict[str, Any]:
            result = self._client.issue(external_id)
            if not isinstance(result, dict):
                raise RuntimeError(f"Unexpected Jira response: {type(result).__name__}")
            return cast(dict[str, Any], result)

        issue = await asyncio.to_thread(_fetch)
        return self._issue_to_task(issue)

    async def transition(self, external_id: str, to_status: str) -> None:
        def _run() -> None:
            self._client.set_issue_status(external_id, to_status)

        await asyncio.to_thread(_run)
        logger.info("Jira {} transitioned to {!r}", external_id, to_status)

    async def comment(self, external_id: str, body: str) -> None:
        def _run() -> None:
            self._client.issue_add_comment(external_id, body)

        await asyncio.to_thread(_run)
        logger.info("Jira {} commented ({} chars)", external_id, len(body))

    # --- internals ---

    def _issue_to_task(self, issue: dict[str, Any]) -> Task:
        key = str(issue["key"])
        fields = cast(dict[str, Any], issue.get("fields") or {})

        priority_name = ""
        if isinstance(fields.get("priority"), dict):
            priority_name = str(fields["priority"].get("name", ""))
        priority = _PRIORITY_MAP.get(priority_name.lower(), TaskPriority.MEDIUM)

        assignee_id: str | None = None
        if isinstance(fields.get("assignee"), dict):
            assignee_id = fields["assignee"].get("name") or fields["assignee"].get("accountId")

        reporter_id: str | None = None
        if isinstance(fields.get("reporter"), dict):
            reporter_id = fields["reporter"].get("name") or fields["reporter"].get("accountId")

        components: list[str] = []
        for c in fields.get("components") or []:
            if isinstance(c, dict) and c.get("name"):
                components.append(str(c["name"]))

        labels: list[str] = [str(label) for label in (fields.get("labels") or [])]

        status_name = ""
        if isinstance(fields.get("status"), dict):
            status_name = str(fields["status"].get("name", ""))

        return Task(
            external_id=key,
            tracker="jira",
            title=str(fields.get("summary", "")),
            description=str(fields.get("description") or ""),
            url=f"{self._browse_base_url}/browse/{key}",
            assignee_id=assignee_id,
            reporter_id=reporter_id,
            components=components,
            labels=labels,
            links=_extract_links(fields),
            priority=priority,
            external_status=status_name,
            created_at=_parse_jira_datetime(fields.get("created")),
            updated_at=_parse_jira_datetime(fields.get("updated")),
            internal_status=TaskStatus.DISCOVERED,
        )


def _extract_links(fields: dict[str, Any]) -> list[TaskLink]:
    """Best-effort: pull remote links Jira ships in issue fields."""
    links: list[TaskLink] = []
    for link in fields.get("issuelinks") or []:
        if not isinstance(link, dict):
            continue
        for side in ("outwardIssue", "inwardIssue"):
            related = link.get(side)
            if isinstance(related, dict) and related.get("key"):
                links.append(TaskLink(url=str(related.get("self", "")), kind="jira_issue"))
    return links
