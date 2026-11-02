"""Custom status → who's away (vacation / sick) for review pings."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from virtual_dev.adapters.chat.mattermost import _active_status_emoji


def _props(emoji: str, expires_at: str) -> dict[str, str]:
    return {"customStatus": json.dumps({
        "emoji": emoji, "text": "Отпуск", "duration": "date_and_time",
        "expires_at": expires_at,
    })}


def test_vacation_status_until_a_future_date_is_active() -> None:
    later = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    assert _active_status_emoji(_props("palm_tree", later)) == "palm_tree"


def test_expired_status_is_ignored() -> None:
    # MM leaves an expired status in props until the user's client clears it.
    earlier = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert _active_status_emoji(_props("palm_tree", earlier)) is None


def test_dont_clear_status_has_zero_date_and_stays_active() -> None:
    assert _active_status_emoji(
        _props("face_with_thermometer", "0001-01-01T00:00:00Z"),
    ) == "face_with_thermometer"


def test_no_status_or_garbage() -> None:
    assert _active_status_emoji({}) is None
    assert _active_status_emoji(None) is None
    assert _active_status_emoji({"customStatus": ""}) is None
    assert _active_status_emoji({"customStatus": "not json"}) is None
