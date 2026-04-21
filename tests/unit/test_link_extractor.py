"""Unit tests for link extraction."""

from __future__ import annotations

from virtual_dev.application.services import extract_links


def test_buckets_by_host() -> None:
    text = """
    Details in https://confluence.2gis.ru/display/DM/Architecture
    and the thread https://mattermost.2gis.ru/datamining/pl/abc123xyz
    related MR https://gitlab.2gis.ru/sd-data-mining/bellingshausen/-/merge_requests/42
    and random https://example.com/whatever
    """
    result = extract_links(
        text,
        confluence_host="confluence.2gis.ru",
        mattermost_host="https://mattermost.2gis.ru",
        gitlab_host="gitlab.2gis.ru",
    )
    assert result.confluence == ["https://confluence.2gis.ru/display/DM/Architecture"]
    assert result.mattermost_threads == ["https://mattermost.2gis.ru/datamining/pl/abc123xyz"]
    assert result.gitlab == [
        "https://gitlab.2gis.ru/sd-data-mining/bellingshausen/-/merge_requests/42"
    ]
    assert result.other == ["https://example.com/whatever"]


def test_root_query_param_counts_as_thread() -> None:
    text = "see https://mm.2gis.ru/team/messages?root=xyz"
    result = extract_links(text, mattermost_host="mm.2gis.ru")
    assert result.mattermost_threads == ["https://mm.2gis.ru/team/messages?root=xyz"]
    assert result.mattermost_messages == []


def test_empty_input() -> None:
    result = extract_links("", confluence_host="c", mattermost_host="m", gitlab_host="g")
    assert result.confluence == []
    assert result.mattermost_threads == []
    assert result.other == []
