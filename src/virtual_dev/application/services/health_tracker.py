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
        self._last_error: dict[str, str] = {}

    def mark_success(self, name: str) -> None:
        self._last_success[name] = self._clock()
        self._last_error.pop(name, None)

    def mark_failure(self, name: str, error: str) -> None:
        """Record that ``name`` just failed. Cleared on next success.

        Failures have no timestamp of their own — the operator-facing
        signal is "this subsystem has an un-cleared error", which is
        sufficient to know something is wrong right now.
        """
        self._last_error[name] = error

    def snapshot(self) -> dict[str, dict[str, Any]]:
        now = self._clock()
        out: dict[str, dict[str, Any]] = {}
        for name, ts in self._last_success.items():
            out[name] = {
                "last_success_at": ts,
                "seconds_since": int((now - ts).total_seconds()),
                "error": self._last_error.get(name),
            }
        # Failures for subsystems that never succeeded (e.g. DB migration
        # on a cold start) must still surface.
        for name, error in self._last_error.items():
            out.setdefault(name, {"error": error})
        return out
