"""Reading CI state: child pipelines, manual triggers, failed jobs."""

from __future__ import annotations

from typing import Any

import pytest

from virtual_dev.adapters.vcs.gitlab import select_triggers
from virtual_dev.application.services.ci_state import read_ci
from virtual_dev.domain.models.merge_request import PipelineJob


def _job(name: str, status: str, **kw: Any) -> PipelineJob:
    return PipelineJob(id=1, name=name, stage="check", status=status, web_url="u", **kw)


class _Vcs:
    def __init__(self, jobs: list[PipelineJob], started: list[str] | Exception) -> None:
        self._jobs = jobs
        self._started = started
        self.play_calls = 0

    async def get_latest_pipeline_jobs(
        self, repo_key: str, iid: int, *, log_tail_lines: int = 0,
    ) -> list[PipelineJob]:
        return self._jobs

    async def play_manual_triggers(self, repo_key: str, iid: int) -> list[str]:
        self.play_calls += 1
        if isinstance(self._started, Exception):
            raise self._started
        return self._started


@pytest.mark.asyncio
async def test_unrun_checks_get_started_and_read_as_running() -> None:
    vcs = _Vcs([_job("01:rapid", "manual", is_trigger=True)], ["01:rapid"])
    reading = await read_ci(vcs, "rainbow", 1031)  # type: ignore[arg-type]
    assert reading.state == "running"
    assert reading.started == ["01:rapid"]


@pytest.mark.asyncio
async def test_nothing_worth_starting_reads_as_no_ci() -> None:
    vcs = _Vcs([_job("deploy", "manual")], [])
    assert (await read_ci(vcs, "r", 1)).state == "no-ci"  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_failed_start_is_unknown_never_green() -> None:
    vcs = _Vcs([_job("01:rapid", "manual", is_trigger=True)], RuntimeError("403"))
    assert (await read_ci(vcs, "r", 1)).state == "unknown"  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_red_child_job_is_failed_and_quality_gate_is_ignored() -> None:
    vcs = _Vcs([
        _job("01:rapid/mypy", "failed"),
        _job("01:rapid/quality_gate", "failed", allow_failure=True),
        _job("01:rapid/build", "success"),
    ], [])
    reading = await read_ci(vcs, "r", 1)  # type: ignore[arg-type]
    assert reading.state == "failed"
    assert [j.name for j in reading.failed_jobs] == ["01:rapid/mypy"]
    assert vcs.play_calls == 0


def test_triggers_named_after_changed_services_are_picked() -> None:
    triggers = ["01:rapid", "02:rainbow-garbage-collector", "03:rainbow-job-schedule-manager"]
    changed = [
        "CLAUDE.md",
        "rainbow-common/rainbow/schemas/jobs.py",
        "rainbow-job-schedule-manager/app/main.py",
        "rapid/app/api/jobs/scheduled/api.py",
    ]
    assert select_triggers(triggers, changed) == [
        "01:rapid", "03:rainbow-job-schedule-manager",
    ]


def test_shared_code_change_starts_every_trigger() -> None:
    triggers = ["01:rapid", "02:rainbow-garbage-collector"]
    assert select_triggers(triggers, ["rainbow-common/x.py"]) == triggers


def test_root_docs_change_starts_nothing() -> None:
    assert select_triggers(["01:rapid"], ["README.md", "AGENTS.md"]) == []
