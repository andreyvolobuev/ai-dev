"""DevOps agent — auto-fixes red CI on bot-authored MRs.

Behaviour per tick (one open MR = one ``_check_one`` call):

  * Fetch latest pipeline jobs WITH FULL logs (no tail truncation).
  * If pipeline is green: reset autofix counter / cleared escalated flag.
    Reviewer's poll will pick the green status up and ping the team
    channel — that's the FIRST and ONLY time the channel hears about
    this MR.
  * If pipeline is red AND attempts < ``max_autofix_attempts``:
    dispatch ``Dev.handle_iteration`` with the failing-jobs feedback
    (job name + stage + URL + full log) as a background task. Counter
    increments after the iteration finishes (not before — we want it
    to reflect actual completed attempts).
  * If pipeline is red AND attempts ≥ max: DM the escalation contact
    once (``pipeline_autofix_escalated`` flag), then stay quiet.

Channels never see CI failures. Period. Notifying the team that the
bot's own commit broke CI is the developer's problem, and the bot is
the developer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents.dev import DevAgent
from virtual_dev.application.services.communicator import CommunicatorService
from virtual_dev.domain.models.merge_request import PipelineJob
from virtual_dev.domain.ports.message_bus import MessageBusPort
from virtual_dev.domain.ports.vcs import VcsPort
from virtual_dev.infrastructure.config import AppConfig
from virtual_dev.infrastructure.db import MergeRequestRow
from virtual_dev.infrastructure.db.base import session_scope


@dataclass
class DevOpsTickStats:
    mrs_checked: int = 0
    failures_detected: int = 0
    autofix_dispatched: int = 0
    escalations_sent: int = 0


class DevOpsAgent:
    agent_key = "devops"

    def __init__(
        self,
        *,
        vcs: VcsPort | None,
        communicator: CommunicatorService,
        session_factory: async_sessionmaker[AsyncSession],
        config: AppConfig,
        dev_agents: dict[str, DevAgent] | None = None,   # repo_key → DevAgent
        message_bus: MessageBusPort | None = None,
    ) -> None:
        self._vcs = vcs
        self._communicator = communicator
        self._session_factory = session_factory
        self._config = config
        self._dev_agents = dev_agents or {}
        self._message_bus = message_bus
        # In-process dedup so two close-together ticks don't dispatch
        # the same iteration twice. Cleared by the background task once
        # it finishes (success or failure).
        self._inflight_autofix: set[tuple[str, int]] = set()
        # Strong refs to the asyncio Tasks we spawn. Without these
        # Python's GC can collect a fire-and-forget task while it's
        # still running; the docs explicitly warn about this. The
        # done-callback discards on completion so this stays bounded.
        self._inflight_tasks: set[asyncio.Task[None]] = set()

    async def tick(self) -> DevOpsTickStats:
        stats = DevOpsTickStats()
        if self._vcs is None:
            return stats

        rows = await self._load_open_mrs()
        stats.mrs_checked = len(rows)

        for row in rows:
            try:
                await self._check_one(row, stats)
            except Exception:
                logger.exception(
                    "DevOps: pipeline check failed for {}!{}", row.repo_key, row.iid,
                )

        return stats

    async def _check_one(self, row: MergeRequestRow, stats: DevOpsTickStats) -> None:
        assert self._vcs is not None
        # log_tail_lines=-1 → full untruncated log per failing job; auto-fix
        # needs the whole traceback (not just trailing frames) to find the
        # real cause. (=0 means "skip log fetch entirely" — used by Reviewer.)
        jobs = list(await self._vcs.get_latest_pipeline_jobs(
            row.repo_key, row.iid, log_tail_lines=-1,
        ))
        pipeline_status = _collapse_status(jobs)

        if pipeline_status == "success":
            # Green. Reset autofix bookkeeping so a future regression starts
            # the counter from 0, and clear the escalated flag.
            await self._persist_pipeline_status(
                row.id, pipeline_status,
                attempts_reset=row.pipeline_autofix_attempts != 0
                or row.pipeline_autofix_escalated,
            )
            return

        if pipeline_status != "failed":
            # pending / running / unknown — just remember the status, don't act.
            await self._persist_pipeline_status(row.id, pipeline_status)
            return

        # --- pipeline is red ---
        stats.failures_detected += 1
        attempts = row.pipeline_autofix_attempts
        max_attempts = self._config.agents.pipeline_policy.max_autofix_attempts

        if attempts >= max_attempts:
            if not row.pipeline_autofix_escalated:
                await self._escalate_via_dm(row, jobs, attempts)
                stats.escalations_sent += 1
                await self._persist_pipeline_status(
                    row.id, pipeline_status, mark_escalated=True,
                )
            else:
                await self._persist_pipeline_status(row.id, pipeline_status)
            return

        # Try auto-fix. Skip if one is already running for this MR.
        key = (row.repo_key, row.iid)
        if key in self._inflight_autofix:
            logger.debug(
                "DevOps: auto-fix already in flight for {}!{}, skipping tick",
                row.repo_key, row.iid,
            )
            await self._persist_pipeline_status(row.id, pipeline_status)
            return

        dev = self._dev_agents.get(row.repo_key)
        if dev is None or not row.task_external_id:
            logger.warning(
                "DevOps: cannot auto-fix {}!{} — dev agent missing or no task; "
                "incrementing attempts to fall through to escalation",
                row.repo_key, row.iid,
            )
            await self._persist_pipeline_status(
                row.id, pipeline_status, increment_attempts=True,
            )
            return

        logger.warning(
            "DevOps: red pipeline on {}!{}, auto-fix attempt {}/{}",
            row.repo_key, row.iid, attempts + 1, max_attempts,
        )
        self._inflight_autofix.add(key)
        stats.autofix_dispatched += 1
        # Fire-and-forget: a single iteration can take minutes; we don't
        # want the poller stalled. The task callback releases the inflight
        # marker and increments the attempts counter.
        task = asyncio.create_task(
            self._run_autofix(row, jobs),
            name=f"devops-autofix-{row.repo_key}-{row.iid}",
        )
        self._inflight_tasks.add(task)
        task.add_done_callback(self._inflight_tasks.discard)
        # Also persist that we observed a red status now (don't increment
        # yet — the task will, after dev finishes).
        await self._persist_pipeline_status(row.id, pipeline_status)

    async def _run_autofix(
        self, row: MergeRequestRow, jobs: list[PipelineJob],
    ) -> None:
        key = (row.repo_key, row.iid)
        try:
            dev = self._dev_agents.get(row.repo_key)
            if dev is None:
                return
            feedback = _render_autofix_feedback(row, jobs)
            commit_sha: str | None = None
            try:
                result = await dev.handle_iteration(
                    tracker="jira",
                    external_id=row.task_external_id or "",
                    branch_name=row.source_branch,
                    feedback=feedback,
                )
                commit_sha = result.commit_sha or None
            except Exception:
                logger.exception(
                    "DevOps: auto-fix iteration crashed for {}!{}",
                    row.repo_key, row.iid,
                )
            # Bump the counter regardless — a crashed iteration also
            # consumed an attempt slot. If the iteration succeeded with
            # a commit, mark the MR as awaiting CI confirmation so the
            # Reviewer poll announces "fixed, CI green" once the new
            # pipeline turns green (no announcement in the meantime).
            await self._post_iteration_state(
                row.id, commit_sha=commit_sha,
            )
        finally:
            self._inflight_autofix.discard(key)

    async def _escalate_via_dm(
        self, row: MergeRequestRow, jobs: list[PipelineJob], attempts: int,
    ) -> None:
        user = await self._resolve_escalation_user()
        if user is None:
            logger.warning(
                "DevOps: no escalation.mattermost_user configured — auto-fix "
                "exhausted on {}!{}, but cannot DM anyone",
                row.repo_key, row.iid,
            )
            return
        failing_names = ", ".join(j.name for j in jobs if j.status == "failed") or "n/a"
        try:
            text = self._config.notifications.mattermost.pipeline_autofix_gave_up_dm.format(
                repo_key=row.repo_key, iid=row.iid,
                title=row.title, web_url=row.web_url,
                attempts=attempts, failing_jobs=failing_names,
            )
        except (KeyError, IndexError) as exc:
            logger.warning("DevOps: pipeline_autofix_gave_up_dm template error: {}", exc)
            text = (
                f"CI на {row.repo_key}!{row.iid} не починился после {attempts} "
                f"попыток. {row.web_url}"
            )
        await self._communicator.send_dm(user, text)

    async def _resolve_escalation_user(self) -> str | None:
        handle = (self._config.agents.escalation.mattermost_user or "").strip()
        if not handle or handle == "your.name":
            return None
        return await self._communicator.resolve_user_id(username=handle)

    async def _load_open_mrs(self) -> list[MergeRequestRow]:
        async with self._session_factory() as session:
            stmt = (
                select(MergeRequestRow)
                .where(MergeRequestRow.status.in_(["open", "draft"]))
                .order_by(MergeRequestRow.created_at.desc())
            )
            return list((await session.execute(stmt)).scalars().all())

    async def _persist_pipeline_status(
        self,
        row_id: int,
        pipeline_status: str,
        *,
        attempts_reset: bool = False,
        mark_escalated: bool = False,
        increment_attempts: bool = False,
    ) -> None:
        async with session_scope(self._session_factory) as session:
            row = (await session.execute(
                select(MergeRequestRow).where(MergeRequestRow.id == row_id)
            )).scalar_one_or_none()
            if row is None:
                return
            row.pipeline_status = pipeline_status
            if attempts_reset:
                row.pipeline_autofix_attempts = 0
                row.pipeline_autofix_escalated = False
            if mark_escalated:
                row.pipeline_autofix_escalated = True
            if increment_attempts:
                row.pipeline_autofix_attempts = (row.pipeline_autofix_attempts or 0) + 1

    async def _post_iteration_state(
        self, row_id: int, *, commit_sha: str | None,
    ) -> None:
        """Bump autofix attempts; remember sha awaiting CI green (if any)."""
        async with session_scope(self._session_factory) as session:
            row = (await session.execute(
                select(MergeRequestRow).where(MergeRequestRow.id == row_id)
            )).scalar_one_or_none()
            if row is None:
                return
            row.pipeline_autofix_attempts = (row.pipeline_autofix_attempts or 0) + 1
            if commit_sha:
                row.iteration_pending_ci_sha = commit_sha


_PASSING_JOB_STATUSES = frozenset({
    # Job ran and produced its expected outcome.
    "success", "skipped",
    # `manual` and `created` are not "running" — they're idle, waiting for
    # someone to click (typical for deploy / approval gates, downstream of
    # merge). For the "ready for review" gate they don't block: review is
    # about code quality, not about whether a deploy gate has been
    # triggered. DevOps treats them as green.
    "manual", "created",
})

_RUNNING_JOB_STATUSES = frozenset({"running", "pending", "preparing", "scheduled"})


def _collapse_status(jobs: list[PipelineJob]) -> str:
    """Derive a single pipeline state from job statuses."""
    if not jobs:
        return "unknown"
    statuses = {j.status for j in jobs}
    if "failed" in statuses:
        return "failed"
    if statuses <= _PASSING_JOB_STATUSES:
        return "success"
    if statuses & _RUNNING_JOB_STATUSES:
        return "running"
    return "unknown"


def _render_autofix_feedback(
    row: MergeRequestRow, jobs: list[PipelineJob],
) -> str:
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
    failing = [j for j in jobs if j.status == "failed"]
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


__all__ = ["DevOpsAgent", "DevOpsTickStats"]
