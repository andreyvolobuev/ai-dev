"""What the CI of an MR says right now — one reading for every agent.

Reviewer (is it green yet?), DevOps (is it red?) and the thread listener
(what do I tell the human?) must see the same state, or the bot says
«CI зелёный» in one place and fixes red jobs in another.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from loguru import logger

from virtual_dev.domain.models.merge_request import PipelineJob
from virtual_dev.domain.ports.vcs import VcsPort


class _MrRef(Protocol):
    repo_key: str
    iid: int
    source_branch: str


_PASSING_JOB_STATUSES = frozenset({
    # Job ran and produced its expected outcome.
    "success", "skipped",
    # `manual` and `created` are not "running" — they're idle, waiting for
    # someone to click (typical for deploy / approval gates, downstream of
    # merge). For the "ready for review" gate they don't block: review is
    # about code quality, not about whether a deploy gate has been
    # triggered. DevOps treats them as green — but only next to jobs
    # that actually ran (see ``_collapse_status``).
    "manual", "created",
})

_RUNNING_JOB_STATUSES = frozenset({"running", "pending", "preparing", "scheduled"})


def _collapse_status(jobs: list[PipelineJob]) -> str:
    """Derive a single pipeline state from job statuses.

    Failed ``allow_failure`` jobs (cleanup / destroy-env gates) don't make
    GitLab mark the pipeline red, so they must not make DevOps see red
    either — there is nothing in the code for an auto-fix to fix.

    ``"manual"`` — nothing ran at all, every job waits for a click. In
    rainbow that is every fresh MR pipeline: the checks sit behind manual
    child-pipeline triggers. Reading it as green announced «CI зелёный»
    three times on code no check had seen (!1031).
    """
    if not jobs:
        return "unknown"
    statuses = {
        "success" if j.status == "failed" and j.allow_failure else j.status
        for j in jobs
    }
    if "failed" in statuses:
        return "failed"
    if statuses <= _PASSING_JOB_STATUSES:
        return "success" if "success" in statuses else "manual"
    if statuses & _RUNNING_JOB_STATUSES:
        return "running"
    return "unknown"


@dataclass(frozen=True)
class CiReading:
    """``state`` is one of ``success`` / ``failed`` / ``running`` /
    ``unknown`` / ``no-ci``."""

    state: str
    jobs: list[PipelineJob] = field(default_factory=list)
    # Manual triggers this reading started (the checks they gate had
    # never run on the latest commit).
    started: list[str] = field(default_factory=list)

    @property
    def failed_jobs(self) -> list[PipelineJob]:
        return [j for j in self.jobs if j.status == "failed" and not j.allow_failure]


async def read_ci(
    vcs: VcsPort, repo_key: str, iid: int, *, log_tail_lines: int = 0,
) -> CiReading:
    """Read the latest pipeline; start its manual checks if none ran.

    A pipeline where every job waits for a click has checked nothing, so
    it is neither green nor red: the bot starts the triggers covering its
    change — what a human developer clicks — and reports ``running``.
    Nothing worth starting (a docs-only change) reads as ``no-ci``.
    """
    try:
        jobs = list(await vcs.get_latest_pipeline_jobs(
            repo_key, iid, log_tail_lines=log_tail_lines,
        ))
    except Exception:
        logger.exception("read_ci: get_latest_pipeline_jobs failed for {}!{}", repo_key, iid)
        return CiReading(state="unknown")
    if not jobs:
        return CiReading(state="no-ci")
    state = _collapse_status(jobs)
    if state != "manual":
        return CiReading(state=state, jobs=jobs)
    try:
        started = await vcs.play_manual_triggers(repo_key, iid)
    except Exception:
        # Not "no-ci": that would announce green on code nothing checked.
        logger.exception("read_ci: starting manual triggers failed for {}!{}", repo_key, iid)
        return CiReading(state="unknown", jobs=jobs)
    return CiReading(state="running" if started else "no-ci", jobs=jobs, started=started)


def render_failed_jobs(row: _MrRef, jobs: list[PipelineJob]) -> str:
    """Build the feedback string Dev sees in iteration mode.

    Includes every failing job's name, stage, URL and FULL log (Dev needs
    the start of the log to find import errors / syntax problems, not
    just the trailing AssertionError).
    """
    lines: list[str] = []
    lines.append(
        f"The CI pipeline on MR {row.repo_key}!{row.iid} (branch "
        f"`{row.source_branch}`) is FAILING. Read the failing jobs' logs "
        "below, find the root cause, fix it in the code, and call "
        "`submit_mr` so the runtime pushes a new commit.",
    )
    lines.append("")
    # allow_failure jobs (quality gates, env cleanup) don't make the
    # pipeline red; sending Dev after them wastes the attempt.
    failing = [j for j in jobs if j.status == "failed" and not j.allow_failure]
    if not failing:
        lines.append("(No failing jobs in the latest pipeline payload — "
                     "this should not happen; investigate manually.)")
        return "\n".join(lines)
    for job in failing:
        lines.append(f"## Job `{job.name}` (stage `{job.stage}`)")
        lines.append(f"URL: {job.web_url}")
        lines.append("")
        lines.append("```")
        lines.append(job.log_excerpt or "(no log available)")
        lines.append("```")
        lines.append("")
    return "\n".join(lines)
