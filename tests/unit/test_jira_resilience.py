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
async def test_fetch_tasks_logs_preview_on_non_json_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the proxy returns an HTML error page, the adapter
    surfaces 'Unexpected Jira response: str'. Make the failure
    diagnosable: the message must include a preview of the body so
    operators can tell (login redirect / 5xx / captcha / ...)."""
    tracker = _build()

    # Patch the underlying client.jql to return a string the way
    # atlassian-python-api does when the response isn't JSON.
    fake_html = (
        "<html><head><title>503 Service Unavailable</title></head>"
        "<body>upstream timed out</body></html>"
    )
    with (
        mock.patch.object(tracker._client, "jql", return_value=fake_html),  # type: ignore[attr-defined]
        pytest.raises(RuntimeError) as exc_info,
    ):
        await tracker.fetch_tasks("project = X")

    msg = str(exc_info.value)
    assert "str" in msg                           # current type signal
    assert "503" in msg or "Service Unavailable" in msg  # body preview reaches operator


def _adapter_for(session: Any, url: str) -> Any:
    return session.get_adapter(url)
