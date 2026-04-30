"""Smoke test: ``/activity`` renders the live-trace template.

The websocket itself isn't tested here — the AgentTrace fan-out is
covered in :mod:`tests.unit.test_agent_trace`. We only check that the
HTML route is wired and the nav link appears so a future refactor
doesn't silently drop the tab.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from virtual_dev.infrastructure.container import build_container
from virtual_dev.presentation.web.app import create_app


@pytest.fixture()
def app_client() -> TestClient:
    container = build_container()
    app = create_app(container, start_scheduler=False)
    return TestClient(app)


def test_activity_page_renders(app_client: TestClient) -> None:
    r = app_client.get("/activity")
    assert r.status_code == 200
    # Feed container + websocket URL the page connects to.
    assert 'id="activity"' in r.text
    assert "/ws/activity" in r.text


def test_activity_link_in_nav(app_client: TestClient) -> None:
    r = app_client.get("/activity")
    assert r.status_code == 200
    # Nav link is in base.html; appearing here proves the new tab is
    # discoverable from anywhere in the app.
    assert "/activity" in r.text
    assert "Активность" in r.text
