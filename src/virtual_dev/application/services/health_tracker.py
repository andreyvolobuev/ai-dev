"""Per-subsystem last-success timestamps, surfaced via /healthz.

After a long network outage the bot was alive but blind — the
process kept ticking, but every Jira call had been failing for
hours and the dashboard had no way to tell. Subsystems mark a
success after each completed call; the dashboard reads the snapshot
and shows "alive but Jira hasn't worked in N minutes".

Intentionally minimal — just last_success_at per name. Failure
counts / error histograms are out of scope; this is the bare signal
operators need to know whether to investigate.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class HealthTracker:
    """Thread-unsafe but asyncio-safe (all updates from the event loop)."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or _utcnow
        self._last_success: dict[str, datetime] = {}

    def mark_success(self, name: str) -> None:
        self._last_success[name] = self._clock()

    def snapshot(self) -> dict[str, dict[str, Any]]:
        now = self._clock()
        return {
            name: {
                "last_success_at": ts,
                "seconds_since": int((now - ts).total_seconds()),
            }
            for name, ts in self._last_success.items()
        }
