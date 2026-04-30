"""Jira adapter (self-hosted) via ``atlassian-python-api``.

Synchronous under the hood — we wrap calls in ``asyncio.to_thread`` so the
orchestrator stays fully async.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from atlassian import Jira
from loguru import logger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from virtual_dev.domain.models.task import (
    Task,
    TaskComment,
    TaskLink,
    TaskPriority,
    TaskStatus,
)
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
    # Cloud / modern Jira (>=9): "2025-03-05T10:20:30.000+03:00" / "...Z".
    # Python 3.11+ fromisoformat handles both directly.
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        pass
    # Self-hosted Jira < 9: "2025-03-05T10:20:30.000+0300" — same offset,
    # no colon. Insert one so fromisoformat accepts it. Match the TZ
    # suffix explicitly so we don't mangle other malformed inputs.
    m = re.fullmatch(r"(.+?)([+-])(\d{2})(\d{2})", raw)
    if m:
        head, sign, hh, mm = m.groups()
        try:
            return datetime.fromisoformat(f"{head}{sign}{hh}:{mm}")
        except ValueError:
            pass
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
        # Mount a Retry-aware adapter on the underlying requests
        # session. Default HTTPAdapter does no retries — so a single
        # stale TCP socket after a network blip surfaces as
        # ConnectionResetError instead of being transparently
        # reconnected. See test_jira_resilience for the contract.
        _install_retry_adapter(self._client._session)
        # For building web URLs to tickets.
        self._browse_base_url = (browse_base_url or url).rstrip("/")

    async def fetch_tasks(self, jql: str, limit: int = 50) -> Sequence[Task]:
        def _fetch() -> list[dict[str, Any]]:
            result = self._client.jql(jql, limit=limit)
            if not isinstance(result, dict):
                raise RuntimeError(_unexpected_response_message(result))
            return cast(list[dict[str, Any]], result.get("issues", []))

        issues = await asyncio.to_thread(_fetch)
        return [self._issue_to_task(issue) for issue in issues]

    async def get_task(self, external_id: str) -> Task:
        def _fetch() -> dict[str, Any]:
            result = self._client.issue(external_id)
            if not isinstance(result, dict):
                raise RuntimeError(_unexpected_response_message(result))
            return cast(dict[str, Any], result)

        issue = await asyncio.to_thread(_fetch)
        task = self._issue_to_task(issue)
        # Single-ticket path also enriches with remote links (Confluence
        # "mentioned in" etc.) and comments (often carry the MM-thread
        # link / brief / explicit "ask X" directives the reporter
        # didn't fit in the description). Skipped in the batch
        # ``fetch_tasks`` path — the per-issue REST round-trips would
        # multiply discovery cost. Failures degrade gracefully — we
        # still return the task, just without that enrichment.
        try:
            remote_links = await asyncio.to_thread(
                _fetch_remote_links, self._client, external_id,
            )
            task.links.extend(remote_links)
        except Exception:
            logger.exception(
                "Jira.get_task({}): remote-link fetch failed; continuing "
                "without remote_link entries", external_id,
            )
        try:
            task.comments = await asyncio.to_thread(
                _fetch_comments, self._client, external_id,
            )
        except Exception:
            logger.exception(
                "Jira.get_task({}): comment fetch failed; continuing "
                "without comments", external_id,
            )
        return task

    async def transition(self, external_id: str, to_status: str) -> None:
        """Transition an issue to the given target status.

        Looks up the workflow transition whose ``to`` matches ``to_status``
        (case-insensitive) and POSTs it by id. atlassian-python-api's
        ``set_issue_status`` does the same lookup internally but reports
        a confusing ``transition identifier must be an integer`` error
        when no match is found — we replace that with a clear message
        listing the available transitions so config can be fixed quickly.
        """
        def _run() -> None:
            transitions = self._client.get_issue_transitions(external_id) or []
            target = to_status.strip().lower()
            match = None
            for t in transitions:
                if str(t.get("to") or "").strip().lower() == target:
                    match = t
                    break
            if match is None:
                available = ", ".join(
                    f"{t.get('name')!r}→{t.get('to')!r}" for t in transitions
                ) or "(none)"
                raise RuntimeError(
                    f"Jira {external_id}: no workflow transition with target "
                    f"{to_status!r}. Available: {available}"
                )
            self._client.set_issue_status_by_transition_id(external_id, match["id"])

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
            links=_extract_links(fields, browse_base_url=self._browse_base_url),
            priority=priority,
            external_status=status_name,
            created_at=_parse_jira_datetime(fields.get("created")),
            updated_at=_parse_jira_datetime(fields.get("updated")),
            internal_status=TaskStatus.DISCOVERED,
        )


def _extract_links(fields: dict[str, Any], *, browse_base_url: str = "") -> list[TaskLink]:
    """Best-effort: pull links Jira ships inside the issue payload.

    Two kinds emitted here, both from data inline in the issue JSON
    (no extra REST round-trips): ``jira_issue`` (issuelinks) and
    ``jira_attachment``. Remote links (Jira ⇄ Confluence
    back-references) live on a separate endpoint and are added by
    :func:`_fetch_remote_links` from ``get_task``.
    """
    links: list[TaskLink] = []
    for link in fields.get("issuelinks") or []:
        if not isinstance(link, dict):
            continue
        link_type = link.get("type") or {}
        for side, label_key in (
            ("outwardIssue", "outward"), ("inwardIssue", "inward"),
        ):
            related = link.get(side)
            if not isinstance(related, dict) or not related.get("key"):
                continue
            related_fields = related.get("fields") or {}
            related_status = related_fields.get("status") or {}
            key = str(related["key"])
            url = (
                f"{browse_base_url}/browse/{key}"
                if browse_base_url
                else str(related.get("self", ""))
            )
            links.append(TaskLink(
                url=url,
                kind="jira_issue",
                external_id=key,
                relationship=str(link_type.get(label_key) or "linked"),
                summary=str(related_fields.get("summary") or "") or None,
                status=str(related_status.get("name") or "") or None,
            ))
    # Attachments: Jira returns each as {id, filename, content (URL),
    # mimeType, size}. We surface name + URL so the right
    # ``read_<format>_url`` tool can fetch (host-aware auth picks the
    # Jira PAT automatically). Without this block the analyst would
    # have to guess at the attachment URL from the description's
    # ``[^filename]`` shorthand and would hallucinate one.
    for att in fields.get("attachment") or []:
        if not isinstance(att, dict):
            continue
        att_id = att.get("id")
        if att_id is None:
            continue
        links.append(TaskLink(
            url=str(att.get("content") or ""),
            kind="jira_attachment",
            name=str(att.get("filename") or ""),
            external_id=str(att_id),
        ))
    return links


def _fetch_remote_links(client: Jira, key: str) -> list[TaskLink]:
    """Hit ``/rest/api/2/issue/<key>/remotelink`` and turn the response
    into ``TaskLink(kind="remote_link")`` entries.

    Sync — wrap with ``asyncio.to_thread`` from the call site. Returns
    an empty list if the endpoint is unavailable or empty; never
    raises (the caller logs and continues).

    A typical entry from Jira looks like::

        {
          "id": 435580,
          "globalId": "appId=…&pageId=…",
          "application": {"type": "com.atlassian.confluence", ...},
          "relationship": "mentioned in",
          "object": {"url": "https://confluence.…/pages/…",
                     "title": "Page", ...},
        }

    ``object.title`` is often a generic "Page" — Jira doesn't snapshot
    the real Confluence page title here. The agent should call
    ``fetch_url`` on the URL to get the actual content.
    """
    raw = client.get_issue_remote_links(key) or []
    if not isinstance(raw, list):
        return []
    out: list[TaskLink] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        obj = entry.get("object") or {}
        url = str(obj.get("url") or "").strip()
        if not url:
            continue
        out.append(TaskLink(
            url=url,
            kind="remote_link",
            relationship=str(entry.get("relationship") or "") or None,
            summary=str(obj.get("title") or "") or None,
        ))
    return out


def _fetch_comments(client: Jira, key: str) -> list[TaskComment]:
    """Pull every comment on the ticket via ``/rest/api/2/issue/<key>/comment``.

    Sync — wrap with ``asyncio.to_thread`` from the call site. The
    inline ``fields.comment`` block on the basic ``/issue/<key>``
    response is server-capped (commonly to 1 entry on self-hosted
    Jira), so we use the dedicated endpoint which defaults to
    ``maxResults=1048576`` — effectively all comments. Returns an
    empty list on any error; the caller logs and continues without
    comments.

    Each Jira comment looks like::

        {
          "id": "12345",
          "author": {"displayName": "...", "name": "...", "emailAddress": "..."},
          "body": "raw text — including markdown / wiki-syntax / URLs",
          "created": "2026-04-28T14:25:02.237+0700",
          "updated": "...",
        }
    """
    raw = client.issue_get_comments(key) or {}
    if not isinstance(raw, dict):
        return []
    out: list[TaskComment] = []
    for entry in raw.get("comments") or []:
        if not isinstance(entry, dict):
            continue
        author_obj = entry.get("author") or {}
        author = (
            str(author_obj.get("displayName") or "")
            or str(author_obj.get("name") or "")
            or "(unknown)"
        )
        body = str(entry.get("body") or "").strip()
        if not body:
            continue
        out.append(TaskComment(
            author=author,
            body=body,
            created_at=_parse_jira_datetime(entry.get("created")),
            external_id=str(entry.get("id") or "") or None,
        ))
    return out


def _install_retry_adapter(session: Any) -> None:
    """Mount an HTTPAdapter with Retry on a requests Session.

    Defaults give 3 connect + 3 read retries with exponential backoff,
    plus retry on 502/503/504 (transient proxy/upstream blips).
    Connection-reset / dead-pool sockets after a network blip are
    handled by ``connect`` retries because urllib3 raises a connect
    error when the existing socket is found dead on first use.
    """
    retry = Retry(
        total=5,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1.0,
        status_forcelist=[502, 503, 504],
        allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)


def _unexpected_response_message(result: Any) -> str:
    """Format the 'non-dict response from Jira' error with a body
    preview so the operator can tell what came back (login redirect /
    5xx HTML / captcha / proxy banner / ...). Without the preview the
    raw exception was just ``Unexpected Jira response: str``."""
    type_name = type(result).__name__
    preview = str(result).strip()
    if len(preview) > 200:
        preview = preview[:200] + "..."
    if not preview:
        return f"Unexpected Jira response: {type_name} (empty body)"
    return f"Unexpected Jira response: {type_name}: {preview}"
