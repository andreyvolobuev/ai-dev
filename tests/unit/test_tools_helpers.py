"""Pure-function tests for tools/_helpers.py.

The HTTP and parser helpers around attachment / thread tools have
deterministic logic worth pinning — URL parsing especially, since
Mattermost permalinks come in several shapes.
"""

from __future__ import annotations

import pytest

from virtual_dev.tools._helpers import (
    auth_headers_for,
    is_trusted_internal_host,
    parse_jira_attachment_id,
    parse_mm_post_id,
    url_is_on_host,
)


@pytest.mark.parametrize(
    ("inp", "expected"),
    [
        ("https://mm.2gis.one/2gis-rd/pl/1f399f976jggpmi96e6qxftw3h",
         "1f399f976jggpmi96e6qxftw3h"),
        ("https://mm.example.com/team/pl/abc123def456ghi789jkl012mn",
         "abc123def456ghi789jkl012mn"),
        # Trailing slash + query string variants
        ("https://mm.2gis.one/2gis-rd/pl/abcdefghij1234567890ab/",
         "abcdefghij1234567890ab"),
        # Bare id pass-through (26 chars, alnum lower)
        ("abcdefghij1234567890abcdef", "abcdefghij1234567890abcdef"),
    ],
)
def test_parse_mm_post_id_extracts_from_permalink_or_bare(
    inp: str, expected: str,
) -> None:
    assert parse_mm_post_id(inp) == expected


@pytest.mark.parametrize(
    "inp",
    [
        "",
        "https://mm.2gis.one/2gis-rd/channels/general",  # not a /pl/ link
        "not a url at all",
        "abc",   # too short to be a bare id
    ],
)
def test_parse_mm_post_id_returns_none_for_unparseable(inp: str) -> None:
    assert parse_mm_post_id(inp) is None


@pytest.mark.parametrize(
    ("inp", "expected"),
    [
        ("https://jira.2gis.ru/secure/attachment/788824/V2%20(2).pdf", "788824"),
        ("https://jira.example.com/secure/attachment/123/file.docx", "123"),
        # Trailing-slash variant
        ("https://jira.example.com/secure/attachment/999/", "999"),
        # Bare id pass-through
        ("788824", "788824"),
    ],
)
def test_parse_jira_attachment_id_extracts_id(inp: str, expected: str) -> None:
    assert parse_jira_attachment_id(inp) == expected


@pytest.mark.parametrize(
    "inp",
    [
        "",
        "https://jira.example.com/browse/DM-123",  # not an attachment URL
        "not a url",
        "abc",  # non-numeric bare
    ],
)
def test_parse_jira_attachment_id_none_for_unparseable(inp: str) -> None:
    assert parse_jira_attachment_id(inp) is None


@pytest.mark.parametrize(
    ("url", "host", "expected"),
    [
        ("https://jira.2gis.ru/secure/attachment/123/file.pdf",
         "jira.2gis.ru", True),
        ("https://jira.2gis.ru/x", "https://jira.2gis.ru", True),
        # Subdomain match via endswith
        ("https://jira.subdomain.example.com/x", "example.com", True),
        # Different host
        ("https://mm.2gis.one/x", "jira.2gis.ru", False),
        ("not-a-url", "anything", False),
        # Empty host arg
        ("https://anything", "", False),
    ],
)
def test_url_is_on_host(url: str, host: str, expected: bool) -> None:
    assert url_is_on_host(url, host) is expected


# ---------------------------------------------------------------- auth dispatch


class _FakeSettings:
    """Tiny stand-in for pydantic Settings: only the attributes
    ``auth_headers_for`` looks at."""

    def __init__(
        self,
        *,
        confluence_url: str = "",
        confluence_user: str = "",
        confluence_token: str = "",
        jira_url: str = "",
        jira_token: str = "",
        mattermost_url: str = "",
        mattermost_token: str = "",
    ) -> None:
        self.confluence_url = confluence_url
        self.confluence_user = confluence_user
        self.confluence_token = confluence_token
        self.jira_url = jira_url
        self.jira_token = jira_token
        self.mattermost_url = mattermost_url
        self.mattermost_token = mattermost_token


def test_auth_headers_for_picks_jira_bearer() -> None:
    """Jira host → Bearer with the configured PAT."""
    s = _FakeSettings(jira_url="https://jira.example", jira_token="JIRA-PAT")
    headers = auth_headers_for("https://jira.example/secure/attachment/1/x.pdf", s)
    assert headers == {"Authorization": "Bearer JIRA-PAT"}


def test_auth_headers_for_picks_mattermost_bearer() -> None:
    """MM file URLs are on the MM host → Bearer with MATTERMOST_TOKEN."""
    s = _FakeSettings(mattermost_url="https://mm.example", mattermost_token="MM-T")
    headers = auth_headers_for("https://mm.example/api/v4/files/abcde", s)
    assert headers == {"Authorization": "Bearer MM-T"}


def test_auth_headers_for_picks_confluence_basic() -> None:
    """Confluence host → Basic ``user:secret`` (Server/DC PATs use Basic)."""
    s = _FakeSettings(
        confluence_url="https://confluence.example",
        confluence_user="me@example.com",
        confluence_token="cnf-secret",
    )
    headers = auth_headers_for("https://confluence.example/pages/x", s)
    assert "Authorization" in headers
    assert headers["Authorization"].startswith("Basic ")


def test_auth_headers_for_unknown_host_unauthenticated() -> None:
    """Unknown hosts (public web) get no auth — we don't leak PATs."""
    s = _FakeSettings(jira_url="https://jira.example", jira_token="JIRA-PAT")
    assert auth_headers_for("https://example.com/page", s) == {}


def test_auth_headers_for_confluence_skipped_when_creds_incomplete() -> None:
    """If only ``CONFLUENCE_URL`` is set but user/token are empty, we
    don't emit a malformed Basic header — fall through to no auth."""
    s = _FakeSettings(confluence_url="https://confluence.example")
    assert auth_headers_for("https://confluence.example/x", s) == {}


def test_is_trusted_internal_host_matches_configured_services() -> None:
    """The SSRF guard exempts hosts the operator put a PAT in for —
    corporate Jira / Confluence / MM routinely resolve to RFC1918
    over VPN, so blocking them on private-IP grounds would block the
    hosts the agent needs most."""
    s = _FakeSettings(
        jira_url="https://jira.example",
        confluence_url="https://confluence.example",
        mattermost_url="https://mm.example",
    )
    assert is_trusted_internal_host("https://jira.example/x", s)
    assert is_trusted_internal_host("https://confluence.example/y", s)
    assert is_trusted_internal_host("https://mm.example/z", s)
    assert not is_trusted_internal_host("https://example.com/anywhere", s)
