"""Pure-function tests for tools/_helpers.py.

The HTTP and parser helpers around attachment / thread tools have
deterministic logic worth pinning — URL parsing especially, since
Mattermost permalinks come in several shapes.
"""

from __future__ import annotations

import pytest

from virtual_dev.tools._helpers import (
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
