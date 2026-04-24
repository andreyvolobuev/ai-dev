"""DevOps agent — watches CI pipelines on bot-authored MRs.

Responsibilities per tick:

    1. For each open MR in our DB, ask the VCS for the latest pipeline's
       jobs + failing-job log tails.
    2. When the pipeline flips red (failed) and we haven't already notified
       about that state, post a summary comment on the MR and ping the team
       channel via Communicator. One notification per "red → red or green → red"
       transition — subsequent ticks with the same red pipeline stay quiet
       so we don't spam.
    3. When a previously-red pipeline turns green, clear the notified flag
       so a future red will fire again.

No auto-fix in v1 — that's a deep can of worms (credential handling,
retry semantics, cost). The bot posts a diagnosis-friendly comment and
leaves the fix to the human (or, later, to a dedicated agent).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents.orchestrator import TOPIC_PIPELINE_FAILED
from virtual_dev.application.services.communicator import CommunicatorService
from virtual_dev.domain.models.merge_request import PipelineJob
from virtual_dev.domain.ports.message_bus import AgentMessage, MessageBusPort
from virtual_dev.domain.ports.vcs import VcsPort
from virtual_dev.infrastructure.config import AppConfig
from virtual_dev.infrastructure.db import MergeRequestRow
from virtual_dev.infrastructure.db.base import session_scope


@dataclass
class DevOpsTickStats:
    mrs_checked: int = 0
    failures_detected: int = 0
    notifications_sent: int = 0


class DevOpsAgent:
    agent_key = "devops"

    def __init__(
        self,
        *,
        vcs: VcsPort | None,
        communicator: CommunicatorService,
        session_factory: async_sessionmaker[AsyncSession],
        config: AppConfig,
        message_bus: MessageBusPort | None = None,
        log_tail_lines: int = 80,
    ) -> None:
        self._vcs = vcs
        self._communicator = communicator
        self._session_factory = session_factory
        self._config = config
        self._message_bus = message_bus
        self._log_tail_lines = log_tail_lines

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
        jobs = list(await self._vcs.get_latest_pipeline_jobs(
            row.repo_key, row.iid, log_tail_lines=self._log_tail_lines,
        ))
        pipeline_status = _collapse_status(jobs)

        new_red = pipeline_status == "failed" and row.last_pipeline_notified_status != "failed"
        recovered = (
            pipeline_status == "success"
            and row.last_pipeline_notified_status == "failed"
        )

        if new_red:
            failing = [j for j in jobs if j.status == "failed"]
            summary = _render_pipeline_comment(row, failing)
            logger.warning(
                "DevOps: red pipeline on {}!{}; posting to MR + MM",
                row.repo_key, row.iid,
            )
            stats.failures_detected += 1
            if self._communicator is not None:
                channel = self._team_channel_for(row.repo_key)
                mm_text = (
                    f"[virtual-dev] Pipeline FAILED on `{row.repo_key}!{row.iid}`: "
                    f"{row.web_url}\n\n"
                    f"Failing jobs: {', '.join(j.name for j in failing) or 'n/a'}"
                )
                if channel:
                    outcome = await self._communicator.send_channel(channel, mm_text)
                    if outcome.sent:
                        stats.notifications_sent += 1
                else:
                    user = await self._resolve_escalation_user()
                    if user:
                        outcome = await self._communicator.send_dm(user, mm_text)
                        if outcome.sent:
                            stats.notifications_sent += 1
            if self._message_bus is not None:
                await self._message_bus.publish(AgentMessage(
                    id=f"pipeline-failed-{row.repo_key}-{row.iid}-{int(datetime.now(timezone.utc).timestamp())}",
                    from_agent=self.agent_key,
                    to_agent=self.agent_key,
                    topic=TOPIC_PIPELINE_FAILED,
                    payload={
                        "repo_key": row.repo_key,
                        "iid": row.iid,
                        "failing_jobs": [j.name for j in failing],
                    },
                ))
            # Leave the MR-side comment to the caller's discretion; posting
            # through VcsPort.reply_to_comment would need a discussion id.
            # A top-level MR note would need another API method — skipping
            # for v1 to keep scope tight. The MM ping is enough to get eyes.

        await self._persist_tick_state(
            row_id=row.id,
            pipeline_status=pipeline_status,
            clear_notification=recovered,
        )

    async def _resolve_escalation_user(self) -> str | None:
        handle = (self._config.agents.escalation.mattermost_user or "").strip()
        if not handle or handle == "your.name":
            return None
        return await self._communicator.resolve_user_id(username=handle)

    def _team_channel_for(self, repo_key: str) -> str | None:
        mapping = self._config.mappings.team_channels or {}
        return mapping.get(repo_key) or mapping.get("default") or None

    async def _load_open_mrs(self) -> list[MergeRequestRow]:
        async with self._session_factory() as session:
            stmt = (
                select(MergeRequestRow)
                .where(MergeRequestRow.status.in_(["open", "draft"]))
                .order_by(MergeRequestRow.created_at.desc())
            )
            return list((await session.execute(stmt)).scalars().all())

    async def _persist_tick_state(
        self,
        *,
        row_id: int,
        pipeline_status: str,
        clear_notification: bool,
    ) -> None:
        async with session_scope(self._session_factory) as session:
            row = (await session.execute(
                select(MergeRequestRow).where(MergeRequestRow.id == row_id)
            )).scalar_one_or_none()
            if row is None:
                return
            row.pipeline_status = pipeline_status
            if clear_notification:
                row.last_pipeline_notified_status = None
            elif pipeline_status == "failed":
                row.last_pipeline_notified_status = "failed"


def _collapse_status(jobs: list[PipelineJob]) -> str:
    """Derive a single pipeline state from job statuses.

    GitLab exposes `pipeline.status` directly on the MR payload but our
    port already returns jobs — deriving here keeps us honest about what
    the user sees (pipeline reds when any job is red).
    """
    if not jobs:
        return "unknown"
    statuses = {j.status for j in jobs}
    if "failed" in statuses:
        return "failed"
    if statuses <= {"success", "skipped", "manual"}:
        return "success"
    if "running" in statuses or "pending" in statuses or "created" in statuses:
        return "running"
    return "unknown"


def _render_pipeline_comment(row: MergeRequestRow, failing: list[PipelineJob]) -> str:
    lines: list[str] = []
    lines.append(
        f"[virtual-dev] DevOps: pipeline failed on `{row.repo_key}!{row.iid}`."
    )
    lines.append("")
    for job in failing:
        lines.append(f"*{job.name}* ({job.stage}) — {job.web_url}")
        if job.log_excerpt:
            tail = job.log_excerpt
            if len(tail) > 2000:
                tail = tail[-2000:]
            lines.append("```")
            lines.append(tail)
            lines.append("```")
        lines.append("")
    lines.append(row.web_url)
    return "\n".join(lines)


__all__ = ["DevOpsAgent", "DevOpsTickStats"]
