"""Unit tests for the Jira adapter's link extraction.

Covers the two helpers ``_extract_links`` (issuelinks + attachments,
inline in the issue payload) and ``_fetch_remote_links`` (separate
``/remotelink`` endpoint, parsed into ``TaskLink(kind="remote_link")``).

Pinned cases come from the real DM-3168 / DM-3215 shapes verified
empirically against the live Jira during planning.
"""

from __future__ import annotations

from typing import Any

from virtual_dev.adapters.task_tracker.jira import (
    _extract_links,
    _fetch_comments,
    _fetch_remote_links,
)


def test_extract_links_issuelinks_outward_enriches_metadata() -> None:
    """The outward side of an issuelink carries summary + status inline.
    All of it should land on the resulting TaskLink so the analyst's
    prompt can render «DM-3215 — is linked with — title (Status)»
    without an extra REST round-trip."""
    fields = {
        "issuelinks": [{
            "type": {
                "name": "Linked",
                "outward": "is linked with",
                "inward": "is linked with",
            },
            "outwardIssue": {
                "key": "DM-3215",
                "self": "https://jira.example/rest/api/2/issue/8446417",
                "fields": {
                    "summary": "🎯 Парсер для нового источника",
                    "status": {"name": "To Do"},
                },
            },
        }],
    }
    links = _extract_links(fields, browse_base_url="https://jira.example")
    assert len(links) == 1
    link = links[0]
    assert link.kind == "jira_issue"
    assert link.external_id == "DM-3215"
    assert link.relationship == "is linked with"
    assert link.summary == "🎯 Парсер для нового источника"
    assert link.status == "To Do"
    # URL prefers the browse-base form (human-readable) over the REST
    # ``self`` link — the latter goes to the JSON endpoint which the
    # agent has no use for.
    assert link.url == "https://jira.example/browse/DM-3215"


def test_extract_links_issuelinks_inward_uses_inward_label() -> None:
    """Inbound side: ``inward`` label is the right one to surface
    (e.g. "is blocked by" rather than "blocks")."""
    fields = {
        "issuelinks": [{
            "type": {
                "name": "Blocks",
                "outward": "blocks",
                "inward": "is blocked by",
            },
            "inwardIssue": {
                "key": "DM-1",
                "fields": {
                    "summary": "Upstream",
                    "status": {"name": "In Progress"},
                },
            },
        }],
    }
    links = _extract_links(fields, browse_base_url="https://jira.example")
    assert len(links) == 1
    assert links[0].relationship == "is blocked by"
    assert links[0].external_id == "DM-1"


def test_extract_links_attachments_unchanged() -> None:
    """Pre-existing behaviour: attachments still surface with id + name
    + URL so the right ``read_<format>_url`` tool can fetch them
    (host-aware auth picks the Jira PAT automatically for jira.* hosts)."""
    fields = {
        "attachment": [{
            "id": "12345",
            "filename": "diagram.pdf",
            "content": "https://jira.example/secure/attachment/12345/diagram.pdf",
        }],
    }
    links = _extract_links(fields)
    assert len(links) == 1
    assert links[0].kind == "jira_attachment"
    assert links[0].external_id == "12345"
    assert links[0].name == "diagram.pdf"


def test_fetch_remote_links_parses_confluence_back_references() -> None:
    """The Confluence-Jira app link auto-creates ``mentioned in``
    remote-links. Each one should turn into a ``TaskLink`` with the
    URL preserved verbatim — Jira's ``object.title`` is generic
    ("Page") so we just pass it through and let the analyst call
    ``fetch_url`` on the URL to get the real title + content."""

    class _StubClient:
        def get_issue_remote_links(self, key: str) -> list[dict[str, Any]]:
            assert key == "DM-3168"
            return [
                {
                    "id": 435580,
                    "globalId": "appId=…&pageId=539899447",
                    "application": {
                        "type": "com.atlassian.confluence",
                        "name": "Confluence",
                    },
                    "relationship": "mentioned in",
                    "object": {
                        "url": "https://confluence.example/pages/viewpage.action?pageId=539899447",
                        "title": "Page",
                    },
                },
            ]

    out = _fetch_remote_links(_StubClient(), "DM-3168")  # type: ignore[arg-type]
    assert len(out) == 1
    link = out[0]
    assert link.kind == "remote_link"
    assert link.url == "https://confluence.example/pages/viewpage.action?pageId=539899447"
    assert link.relationship == "mentioned in"
    assert link.summary == "Page"


def test_fetch_comments_parses_all_fields() -> None:
    """Comments often carry the load-bearing context (MM permalinks,
    estimates, "ask X" directives). The parser pulls author /
    body / created / id from each entry; empty-body comments are
    dropped (whitespace-only "edited" markers Jira sometimes emits)."""

    class _StubClient:
        def issue_get_comments(self, key: str) -> dict[str, Any]:
            assert key == "DM-3342"
            return {
                "total": 2,
                "maxResults": 1048576,
                "comments": [
                    {
                        "id": "12345",
                        "author": {
                            "displayName": "Андрей Волобуев",
                            "name": "an.volobuev",
                        },
                        "body": "Обсуждение в Mattermost: https://mm/pl/abc",
                        "created": "2026-04-28T14:25:02.237+0700",
                    },
                    {
                        "id": "12346",
                        "author": {"displayName": "Bob"},
                        "body": "   ",  # whitespace-only — drop
                        "created": "2026-04-28T15:00:00.000+0700",
                    },
                ],
            }

    out = _fetch_comments(_StubClient(), "DM-3342")  # type: ignore[arg-type]
    assert len(out) == 1
    c = out[0]
    assert c.author == "Андрей Волобуев"
    assert c.external_id == "12345"
    assert "Mattermost" in c.body
    assert c.created_at is not None
    assert c.created_at.year == 2026


def test_fetch_comments_handles_unexpected_response() -> None:
    """A Jira hiccup or non-dict response shouldn't crash get_task —
    return an empty list and let the caller log + continue without
    comments."""

    class _StubClient:
        def issue_get_comments(self, key: str) -> Any:
            return None

    out = _fetch_comments(_StubClient(), "X-1")  # type: ignore[arg-type]
    assert out == []


def test_fetch_remote_links_skips_entries_without_url() -> None:
    """Defensive: malformed entries (no object / no URL) are silently
    dropped rather than blowing up the whole task fetch — better to
    return a partial link list than to fail get_task entirely."""

    class _StubClient:
        def get_issue_remote_links(self, key: str) -> list[dict[str, Any]]:
            return [
                {"id": 1},                                       # no object
                {"id": 2, "object": {}},                          # empty url
                {"id": 3, "object": {"url": "https://x/"},
                 "relationship": "mentioned in"},                 # ok
            ]

    out = _fetch_remote_links(_StubClient(), "X-1")  # type: ignore[arg-type]
    assert [link.url for link in out] == ["https://x/"]
