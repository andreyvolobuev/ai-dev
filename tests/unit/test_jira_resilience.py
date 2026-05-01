"""JiraTaskTracker network resilience.

Symptoms from the prod-ish run after a network blip:

* requests.Session held a stale TCP connection from before the
  network went down, returning ``ConnectionResetError [Errno 54]``
  instead of trying a fresh socket. Each Jira call burned ~5-10
  minutes on stacked retries before failing.
* When a transparent proxy returned an HTML error page, the
  atlassian-python-api client returned a string and our adapter
  raised "Unexpected Jira response: str" — without any indication of
  what the body was.

Both addressed: HTTPAdapter with Retry on the underlying session
(handles connection-reset / 5xx automatically), and a log preview
of non-JSON bodies on the explicit error path.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from virtual_dev.adapters.task_tracker.jira import JiraTaskTracker


def _build() -> JiraTaskTracker:
    # Real construction — no network, just sets up the requests Session.
    return JiraTaskTracker(url="https://jira.example/", token="t")


def test_jira_session_has_retry_adapter() -> None:
    """The atlassian-python-api ``_session`` must have an HTTPAdapter
    mounted that retries on connection errors and 5xx — without it,
    the first dead-pool socket after a network blip surfaces as a
    fatal failure instead of a transparent reconnect."""
    tracker = _build()
    session = tracker._client._session  # type: ignore[attr-defined]

    https_adapter = session.get_adapter("https://jira.example/rest/api/2/search")
    retry = https_adapter.max_retries
    # Defaults of ``HTTPAdapter`` are total=0 / connect=0 — not what we want.
    assert retry.total >= 3, f"expected ≥3 retries, got total={retry.total}"
    assert retry.connect >= 3, f"connect retries too low: {retry.connect}"
    assert retry.read >= 3, f"read retries too low: {retry.read}"
    # Retry on transient 5xx — proxies / Jira upstream blips.
    assert 502 in retry.status_forcelist
    assert 503 in retry.status_forcelist
    assert 504 in retry.status_forcelist


@pytest.mark.asyncio
async def test_fetch_tasks_treats_html_response_as_captive_portal() -> None:
    """A captive portal / hotspot login page returns HTML instead of
    JSON. Treat as a network failure (ConnectionError), not as a
    permanent unexpected-response error — orchestrator's existing
    exception handler logs and retries the next tick instead of
    surfacing a confusing RuntimeError per poll."""
    import requests
    tracker = _build()
    captive = (
        '<!DOCTYPE html><html> <head> <meta charset="UTF-8">'
        '<title> Секретный уровень </title></head>'
        '<body>Войдите в Wi-Fi 2GIS</body></html>'
    )

    with (
        mock.patch.object(tracker._client, "jql", return_value=captive),  # type: ignore[attr-defined]
        pytest.raises(requests.ConnectionError) as exc_info,
    ):
        await tracker.fetch_tasks("project = X")

    msg = str(exc_info.value).lower()
    assert "captive" in msg or "html" in msg or "portal" in msg


@pytest.mark.asyncio
async def test_fetch_tasks_logs_preview_on_non_json_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-HTML / non-JSON garbage (truncated body, broken proxy)
    that we can't classify as a captive portal still surfaces with a
    body preview so an operator can diagnose at a glance."""
    tracker = _build()

    # Patch the underlying client.jql to return a string the way
    # atlassian-python-api does when the response isn't JSON.
    # Plain text (no HTML doctype) — falls through captive-portal
    # detection and surfaces as the original RuntimeError preview.
    fake_garbage = "503 Service Unavailable: upstream timed out"
    with (
        mock.patch.object(tracker._client, "jql", return_value=fake_garbage),  # type: ignore[attr-defined]
        pytest.raises(RuntimeError) as exc_info,
    ):
        await tracker.fetch_tasks("project = X")

    msg = str(exc_info.value)
    assert "str" in msg                           # current type signal
    assert "503" in msg or "Service Unavailable" in msg  # body preview reaches operator


@pytest.mark.asyncio
async def test_fetch_tasks_recycles_session_pool_on_captive_html() -> None:
    """A poisoned TCP socket in the pool keeps returning captive-portal
    HTML across reuse — internal session-level retries hit the same
    dead socket. On HTML detection we MUST clear the connection pool
    so the next tick opens fresh sockets, otherwise the bot spams the
    same error indefinitely (until restart) even after the network
    fully recovers."""
    import requests

    tracker = _build()
    captive = (
        '<!DOCTYPE html><html><head><title>Секретный уровень</title>'
        '</head><body>x</body></html>'
    )

    # Track close() on every adapter mounted on the session — we don't
    # care which exact one (http:// vs https://), just that the pool
    # was purged at least once.
    session = tracker._client._session  # type: ignore[attr-defined]
    close_counts = {scheme: 0 for scheme in session.adapters}
    real_closes = {}
    for scheme, adapter in session.adapters.items():
        real_closes[scheme] = adapter.close

        def _wrapped(_scheme: str = scheme) -> None:
            close_counts[_scheme] += 1

        adapter.close = _wrapped  # type: ignore[method-assign]

    try:
        with (
            mock.patch.object(tracker._client, "jql", return_value=captive),  # type: ignore[attr-defined]
            pytest.raises(requests.ConnectionError),
        ):
            await tracker.fetch_tasks("project = X")
    finally:
        for scheme, adapter in session.adapters.items():
            adapter.close = real_closes[scheme]  # type: ignore[method-assign]

    assert sum(close_counts.values()) >= 1, (
        f"expected at least one adapter.close() to clear the pool; "
        f"got {close_counts}"
    )


def _adapter_for(session: Any, url: str) -> Any:
    return session.get_adapter(url)
