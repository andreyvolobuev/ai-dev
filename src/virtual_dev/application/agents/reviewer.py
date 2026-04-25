"""Reviewer agent — watches open MRs for human activity.

Responsibilities per poll tick:

    1. For each open MR authored by the bot, fetch its current status,
       comments and approvals via :class:`VcsPort`.
    2. When an MR transitions from draft to open, post "please review"
       to the team channel ONCE (``review_ping_sent`` flag persists).
    3. Observe new comments: update ``last_activity_at`` (so the
       escalation timer resets) and classify for logs. We do NOT forward
       comments to Mattermost — the review conversation lives in GitLab.
       Acting on change-requests / questions (iterating code, replying in
       GitLab) is Phase 4 territory.
    4. When the approvals_count reaches the required threshold, publish
       ``mr.approved`` and post "ready to merge" ONCE.
    5. Apply the escalation policy: stale > ``ping_reviewers_after_hours``
       → channel nag once; stale > ``escalate_after_hours`` → DM the
       escalation contact.

Merging is manual — this agent never calls ``vcs.merge``.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents.orchestrator import (
    TOPIC_MR_APPROVED,
    TOPIC_MR_COMMENT,
    TOPIC_MR_STUCK,
)
from virtual_dev.application.services.communicator import CommunicatorService
from virtual_dev.application.agents.devops import _collapse_status
from virtual_dev.domain.models.merge_request import MRStatus, ReviewComment
from virtual_dev.domain.ports.message_bus import AgentMessage, MessageBusPort
from virtual_dev.domain.ports.vcs import VcsPort
from virtual_dev.infrastructure.config import AppConfig
from virtual_dev.infrastructure.db import MergeRequestRow
from virtual_dev.infrastructure.db.base import session_scope


class CommentClass(str, Enum):
    APPROVAL_HINT = "approval_hint"
    QUESTION = "question"
    CHANGE_REQUEST = "change_request"
    CHATTER = "chatter"


_APPROVAL_KEYWORDS = re.compile(
    r"(?i)(\blgtm\b|\bapproved?\b|\+1\b|\bship\s?it\b|ready to merge)",
)
_CHANGE_REQUEST_KEYWORDS = re.compile(
    r"\b(change|fix|please\s+(update|change|fix|rename|remove|add)|"
    r"needs? change|rework|wrong|rewrite|should (not )?be|don't|do not)\b",
    re.IGNORECASE,
)


def classify_comment(body: str) -> CommentClass:
    """Classify a single comment body. Used for logs; agent does not act
    on the class in Phase 3 (comments are not relayed to Mattermost)."""
    stripped = body.strip()
    if not stripped:
        return CommentClass.CHATTER
    if _APPROVAL_KEYWORDS.search(stripped):
        return CommentClass.APPROVAL_HINT
    if stripped.endswith("?"):
        return CommentClass.QUESTION
    if _CHANGE_REQUEST_KEYWORDS.search(stripped):
        return CommentClass.CHANGE_REQUEST
    return CommentClass.CHATTER


@dataclass
class ReviewerTickStats:
    """Counters returned per poll — handy for the dashboard."""

    mrs_checked: int = 0
    new_comments: int = 0
    review_pings_sent: int = 0
    approvals_sent: int = 0
    pings_sent: int = 0
    escalations_sent: int = 0


class ReviewerAgent:
    """Single-instance agent that scans all repos on each tick."""

    agent_key = "reviewer"

    def __init__(
        self,
        *,
        vcs: VcsPort | None,
        communicator: CommunicatorService,
        session_factory: async_sessionmaker[AsyncSession],
        config: AppConfig,
        message_bus: MessageBusPort | None = None,
        bot_username: str | None = None,
    ) -> None:
        self._vcs = vcs
        self._communicator = communicator
        self._session_factory = session_factory
        self._config = config
        self._message_bus = message_bus
        self._bot_username = (bot_username or "").strip() or None

    # --- Entry point (called by PollerWorker) ---

    async def tick(self) -> ReviewerTickStats:
        stats = ReviewerTickStats()
        if self._vcs is None:
            return stats

        rows = await self._load_open_mrs()
        stats.mrs_checked = len(rows)

        for row in rows:
            try:
                await self._check_one(row, stats)
            except Exception:
                logger.exception("Reviewer: MR {}!{} check failed", row.repo_key, row.iid)

        return stats

    # --- Per-MR logic ---

    async def _check_one(self, row: MergeRequestRow, stats: ReviewerTickStats) -> None:
        assert self._vcs is not None

        # Refresh status from GitLab — the DB copy reflects what Dev wrote
        # when the MR was opened (always draft). We need the current flag
        # to decide whether to post "please review".
        try:
            live = await self._vcs.get_merge_request(row.repo_key, row.iid)
            current_status = live.status.value
            row_status_changed = current_status != row.status
        except Exception:
            logger.exception(
                "Reviewer: get_merge_request failed for {}!{}; skipping",
                row.repo_key, row.iid,
            )
            return

        # Stop tracking MRs that are merged/closed. A future ReviewerAgent
        # could clean them up; for now we just skip the rest of the tick.
        if live.status in (MRStatus.MERGED, MRStatus.CLOSED):
            await self._persist_final_state(row.id, current_status)
            return

        comments = list(await self._vcs.list_review_comments(row.repo_key, row.iid))
        approvals = await self._vcs.get_mr_approvals(row.repo_key, row.iid)

        new_comments = self._new_comments(row, comments)
        stats.new_comments += len(new_comments)

        now = datetime.now(timezone.utc)
        touched = False

        for comment in new_comments:
            klass = classify_comment(comment.body)
            logger.info(
                "Reviewer: new comment on {}!{} from @{} [{}]: {!r}",
                row.repo_key, row.iid, comment.author_username,
                klass.value, comment.body[:160],
            )
            if self._message_bus is not None:
                # UUID, not deterministic comment id — the bus enforces
                # UNIQUE on external_id, so replaying the same comment
                # twice (e.g. after a last_seen reset) must not crash.
                await self._message_bus.publish(AgentMessage(
                    id=uuid.uuid4().hex,
                    from_agent=self.agent_key,
                    to_agent=self.agent_key,
                    topic=TOPIC_MR_COMMENT,
                    payload={
                        "repo_key": row.repo_key,
                        "iid": row.iid,
                        "comment_id": comment.id,
                        "classification": klass.value,
                        "author": comment.author_username,
                    },
                ))
            touched = True

        # "Please review" ping — once, when MR is no longer a draft AND
        # the CI pipeline of the latest commit is green (or there's no
        # CI at all). ``live.pipeline_status`` from get_merge_request is
        # a head-pipeline snapshot that briefly desynchronises right
        # after a push (mr.pipeline may be None or still point at the
        # previous run). We instead pull the latest pipeline's jobs and
        # derive status from them — same source DevOps uses, so both
        # agents see the world the same way.
        ci_state: str | None = None
        review_ping_sent = row.review_ping_sent
        if not review_ping_sent and live.status == MRStatus.OPEN:
            ci_state = await self._derive_ci_state(row.repo_key, row.iid)
            if ci_state == "no-ci":
                # Repo has no pipeline configured for this branch; nothing
                # to wait for, ping right away.
                if await self._notify_ready_for_review(row, live.title):
                    review_ping_sent = True
                    stats.review_pings_sent += 1
                    touched = True
            elif ci_state == "success":
                if await self._notify_ready_for_review(row, live.title):
                    review_ping_sent = True
                    stats.review_pings_sent += 1
                    touched = True
            else:
                logger.info(
                    "Reviewer: {}!{} — CI state {!r}, holding 'please review' ping",
                    row.repo_key, row.iid, ci_state,
                )

        # Iteration follow-up: when an MM-driven or autofix iteration push
        # is awaiting CI confirmation (``iteration_pending_ci_sha`` set),
        # announce "✅ изменения внесены, CI зелёный" in the review thread
        # once CI flips to success. Until then the bot stays silent —
        # reviewers don't want to hear about commits that haven't been
        # validated by CI yet.
        clear_iteration_pending = False
        if row.iteration_pending_ci_sha and row.review_thread_root_id:
            if ci_state is None:
                ci_state = await self._derive_ci_state(row.repo_key, row.iid)
            if ci_state in ("success", "no-ci"):
                await self._announce_iteration_done(row)
                clear_iteration_pending = True
                touched = True

        # Approvals threshold check.
        required = self._config.agents.review_policy.required_approvals
        approvals_required = approvals.required if approvals.required > 0 else required
        approvals_notified = row.approvals_count >= approvals_required
        if approvals.count >= approvals_required and not approvals_notified:
            logger.info(
                "Reviewer: {}!{} reached {} approvals; pinging to merge",
                row.repo_key, row.iid, approvals.count,
            )
            await self._notify_ready_to_merge(row)
            stats.approvals_sent += 1
            if self._message_bus is not None:
                await self._message_bus.publish(AgentMessage(
                    id=uuid.uuid4().hex,
                    from_agent=self.agent_key,
                    to_agent=self.agent_key,
                    topic=TOPIC_MR_APPROVED,
                    payload={
                        "repo_key": row.repo_key,
                        "iid": row.iid,
                        "count": approvals.count,
                    },
                ))
            touched = True

        # Escalation policy: fire only when no human activity this tick.
        escalated_this_tick = False
        if not touched:
            escalated_this_tick = await self._maybe_escalate(row, now, stats)

        # Persist state.
        await self._persist_tick_state(
            row_id=row.id,
            new_status=current_status,
            new_title=live.title,
            new_last_seen=(new_comments[-1].id if new_comments else None),
            approvals_count=approvals.count,
            approvals_required=approvals_required,
            review_ping_sent=review_ping_sent,
            touched=touched,
            escalated_this_tick=escalated_this_tick,
            clear_iteration_pending=clear_iteration_pending,
            now=now,
        )
        if row_status_changed:
            logger.info(
                "Reviewer: {}!{} status transitioned {} → {}",
                row.repo_key, row.iid, row.status, current_status,
            )

    async def _announce_iteration_done(self, row: MergeRequestRow) -> None:
        """Post the post-CI-green ack into the review thread."""
        channel_id = row.review_thread_channel_id
        root_id = row.review_thread_root_id
        if not channel_id or not root_id:
            return
        sha = row.iteration_pending_ci_sha or ""
        try:
            text = self._config.notifications.mattermost.thread_reply_iteration_done.format(
                commit_sha_short=sha[:12], branch=row.source_branch,
            )
        except (KeyError, IndexError):
            text = self._config.notifications.mattermost.thread_reply_iteration_done
        await self._communicator.send_channel(channel_id, text, thread_root_id=root_id)
        logger.info(
            "Reviewer: posted iteration-done ack for {}!{} sha={}",
            row.repo_key, row.iid, sha[:12],
        )

    async def _notify_ready_for_review(
        self, row: MergeRequestRow, live_title: str,
    ) -> bool:
        """Post a one-off "please review" into the team channel.

        Returns True if the ping was sent (persist the flag) and records the
        resulting post id + channel on the MR so the MM thread listener can
        route replies back to us. Returns False if the ping was skipped.
        """
        channel_id = self._team_channel_for(row.repo_key)
        if not channel_id:
            logger.info(
                "Reviewer: no team channel configured for {!r}; skipping review ping",
                row.repo_key,
            )
            return False
        body = self._render_template(
            self._config.notifications.mattermost.review_ping,
            repo_key=row.repo_key, iid=row.iid,
            title=live_title or row.title, web_url=row.web_url,
        )
        outcome = await self._communicator.send_channel(channel_id, body)
        if outcome.sent and outcome.message is not None:
            # Persist thread metadata immediately so a reply that lands
            # before the next tick can still be routed back to this MR.
            async with session_scope(self._session_factory) as session:
                fresh = (await session.execute(
                    select(MergeRequestRow).where(MergeRequestRow.id == row.id)
                )).scalar_one_or_none()
                if fresh is not None:
                    fresh.review_thread_channel_id = outcome.message.channel_id
                    fresh.review_thread_root_id = outcome.message.id
        return outcome.sent

    async def _notify_ready_to_merge(self, row: MergeRequestRow) -> None:
        msg = self._render_template(
            self._config.notifications.mattermost.merge_ping,
            repo_key=row.repo_key, iid=row.iid,
            title=row.title, web_url=row.web_url,
        )
        channel_id = self._team_channel_for(row.repo_key)
        if channel_id:
            await self._communicator.send_channel(channel_id, msg)
            return
        user = await self._resolve_escalation_user()
        if user:
            await self._communicator.send_dm(user, msg)

    async def _maybe_escalate(
        self, row: MergeRequestRow, now: datetime, stats: ReviewerTickStats,
    ) -> bool:
        policy = self._config.agents.review_policy
        last_activity = row.last_activity_at or row.created_at
        idle = now - _aware(last_activity)

        # Escalation first (higher priority; also skips the ping).
        if idle >= timedelta(hours=policy.escalate_after_hours):
            if (
                row.last_escalation_at is None
                or now - _aware(row.last_escalation_at)
                >= timedelta(hours=policy.escalate_after_hours)
            ):
                user = await self._resolve_escalation_user()
                if user:
                    text = self._render_template(
                        self._config.notifications.mattermost.escalation_dm,
                        repo_key=row.repo_key, iid=row.iid,
                        title=row.title, web_url=row.web_url,
                        idle_hours=int(idle.total_seconds() / 3600),
                    )
                    await self._communicator.send_dm(user, text)
                    stats.escalations_sent += 1
                    if self._message_bus is not None:
                        await self._message_bus.publish(AgentMessage(
                            id=uuid.uuid4().hex,
                            from_agent=self.agent_key,
                            to_agent=self.agent_key,
                            topic=TOPIC_MR_STUCK,
                            payload={
                                "repo_key": row.repo_key, "iid": row.iid,
                                "idle_hours": idle.total_seconds() / 3600,
                            },
                        ))
                return True
            return False

        if idle >= timedelta(hours=policy.ping_reviewers_after_hours):
            if row.ping_reviewers_at is None:
                channel_id = self._team_channel_for(row.repo_key)
                text = self._render_template(
                    self._config.notifications.mattermost.stale_ping,
                    repo_key=row.repo_key, iid=row.iid,
                    title=row.title, web_url=row.web_url,
                    idle_hours=int(idle.total_seconds() / 3600),
                )
                if channel_id:
                    await self._communicator.send_channel(channel_id, text)
                    stats.pings_sent += 1
                return True

        return False

    def _render_template(self, template: str, **kwargs: object) -> str:
        """Format a notifications template, swallowing missing keys.

        Defensive against operator typos in config (e.g. ``{idl_hours}``):
        we'd rather post a slightly broken-looking message than crash the
        whole tick on KeyError.
        """
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError) as exc:
            logger.warning(
                "Reviewer: template format failed ({}): {}",
                exc, template[:120],
            )
            return template

    # --- Helpers ---

    def _new_comments(
        self, row: MergeRequestRow, comments: list[ReviewComment],
    ) -> list[ReviewComment]:
        """Return comments the Reviewer hasn't seen yet.

        ``list_review_comments`` returns oldest-first (enforced at the
        adapter). We advance past everything up to and including
        ``last_seen_comment_id``, then return the tail minus any comments
        from a configured ``bot_username`` (empty in single-user setups —
        nothing is filtered).
        """
        filtered: list[ReviewComment] = []
        seen_cutoff = row.last_seen_comment_id or ""
        passed_cutoff = not seen_cutoff
        for comment in comments:
            if not passed_cutoff:
                if comment.id == seen_cutoff:
                    passed_cutoff = True
                continue
            if self._is_bot_author(comment.author_username):
                continue
            filtered.append(comment)
        return filtered

    def _is_bot_author(self, username: str) -> bool:
        if self._bot_username and username.lower() == self._bot_username.lower():
            return True
        return False

    async def _derive_ci_state(self, repo_key: str, iid: int) -> str:
        """Return one of: ``"success"``, ``"failed"``, ``"running"``,
        ``"unknown"``, ``"no-ci"``.

        Pulls the actual job list from the latest pipeline and collapses
        their statuses (same logic DevOps uses), so the result reflects
        ground truth instead of the brief desync window in mr.pipeline
        right after a push.
        """
        assert self._vcs is not None
        try:
            jobs = list(
                await self._vcs.get_latest_pipeline_jobs(repo_key, iid, log_tail_lines=0)
            )
        except Exception:
            logger.exception(
                "Reviewer: get_latest_pipeline_jobs failed for {}!{}", repo_key, iid,
            )
            return "unknown"
        if not jobs:
            return "no-ci"
        return _collapse_status(jobs)

    def _team_channel_for(self, repo_key: str) -> str | None:
        mapping = self._config.mappings.team_channels or {}
        return mapping.get(repo_key) or mapping.get("default") or None

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

    async def _persist_tick_state(
        self,
        *,
        row_id: int,
        new_status: str,
        new_title: str,
        new_last_seen: str | None,
        approvals_count: int,
        approvals_required: int,
        review_ping_sent: bool,
        touched: bool,
        escalated_this_tick: bool,
        clear_iteration_pending: bool,
        now: datetime,
    ) -> None:
        async with session_scope(self._session_factory) as session:
            row = (await session.execute(
                select(MergeRequestRow).where(MergeRequestRow.id == row_id)
            )).scalar_one_or_none()
            if row is None:
                return
            row.status = new_status
            if new_title:
                row.title = new_title
            if new_last_seen:
                row.last_seen_comment_id = new_last_seen
            row.approvals_count = approvals_count
            row.approvals_required = approvals_required
            row.review_ping_sent = review_ping_sent
            if touched:
                row.last_activity_at = now
                row.ping_reviewers_at = None
                row.last_escalation_at = None
            if escalated_this_tick:
                row.last_escalation_at = now
            if clear_iteration_pending:
                row.iteration_pending_ci_sha = None

    async def _persist_final_state(self, row_id: int, new_status: str) -> None:
        async with session_scope(self._session_factory) as session:
            row = (await session.execute(
                select(MergeRequestRow).where(MergeRequestRow.id == row_id)
            )).scalar_one_or_none()
            if row is not None:
                row.status = new_status


def _aware(dt: datetime | None) -> datetime:
    """Normalize possibly-naive SQLite datetimes to UTC-aware ones."""
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


__all__ = ["ReviewerAgent", "ReviewerTickStats", "CommentClass", "classify_comment"]
