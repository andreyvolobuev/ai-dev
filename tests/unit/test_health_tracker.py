"""Health tracker — last-success timestamps per subsystem.

The user-visible motivation: after a long network outage, the bot is
silently retrying / failing every ~17 min. The dashboard had no way
to tell "fully healthy" from "alive but blind". This is the data
layer for /healthz.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from virtual_dev.application.services.health_tracker import HealthTracker


def test_unmarked_subsystem_has_no_last_success() -> None:
    h = HealthTracker()
    snap = h.snapshot()
    assert snap == {}


def test_mark_success_records_timestamp() -> None:
    base = datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc)
    h = HealthTracker(clock=lambda: base)

    h.mark_success("jira_fetch")

    snap = h.snapshot()
    assert "jira_fetch" in snap
    assert snap["jira_fetch"]["last_success_at"] == base
    assert snap["jira_fetch"]["seconds_since"] == 0


def test_seconds_since_advances_with_clock() -> None:
    now_holder = [datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc)]

    def clock() -> datetime:
        return now_holder[0]

    h = HealthTracker(clock=clock)
    h.mark_success("gitlab")

    now_holder[0] = now_holder[0] + timedelta(minutes=5)
    snap = h.snapshot()
    assert snap["gitlab"]["seconds_since"] == 300


def test_repeat_mark_overwrites_previous() -> None:
    now_holder = [datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc)]
    h = HealthTracker(clock=lambda: now_holder[0])

    h.mark_success("jira_fetch")
    now_holder[0] = now_holder[0] + timedelta(minutes=10)
    h.mark_success("jira_fetch")

    snap = h.snapshot()
    assert snap["jira_fetch"]["seconds_since"] == 0


def test_snapshot_independent_per_subsystem() -> None:
    base = datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc)
    now_holder = [base]
    h = HealthTracker(clock=lambda: now_holder[0])

    h.mark_success("jira_fetch")
    now_holder[0] = base + timedelta(seconds=120)
    h.mark_success("gitlab")
    now_holder[0] = base + timedelta(seconds=180)

    snap = h.snapshot()
    assert snap["jira_fetch"]["seconds_since"] == 180
    assert snap["gitlab"]["seconds_since"] == 60


def test_failure_surfaces_even_without_a_success() -> None:
    h = HealthTracker()
    h.mark_failure("db_migration", "timed out after 30s")

    snap = h.snapshot()
    assert "db_migration" in snap
    assert snap["db_migration"]["error"] == "timed out after 30s"
    assert "last_success_at" not in snap["db_migration"]


def test_success_clears_previous_error() -> None:
    base = datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc)
    h = HealthTracker(clock=lambda: base)

    h.mark_failure("db_migration", "boom")
    h.mark_success("db_migration")

    snap = h.snapshot()
    assert snap["db_migration"]["error"] is None
    assert snap["db_migration"]["last_success_at"] == base


def test_error_recorded_alongside_last_success() -> None:
    base = datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc)
    h = HealthTracker(clock=lambda: base)

    h.mark_success("gitlab")
    h.mark_failure("gitlab", "503")

    snap = h.snapshot()
    assert snap["gitlab"]["error"] == "503"
    assert snap["gitlab"]["last_success_at"] == base
