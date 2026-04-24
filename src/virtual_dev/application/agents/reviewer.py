"""Reviewer agent — watches open MRs for human activity.

Responsibilities per poll tick:

    1. For each open MR authored by the bot, fetch its review comments and
       approvals via :class:`VcsPort`. Skip comments authored by the bot
       itself — we only react to humans.
    2. Diff the fetched comments against ``last_seen_comment_id`` on the MR
       row so each comment is processed at most once.
    3. Classify new comments (approval hint / question / change-request /
       chatter) and let the Communicator relay interesting ones to Mattermost.
    4. When the approvals_count reaches the configured threshold, publish
       ``mr.approved`` and post a "ready to merge" ping.
    5. Apply the escalation policy: if no activity for
       ``ping_reviewers_after_hours`` → ping reviewers once; for
       ``escalate_after_hours`` → DM the escalation contact.

Merging is deliberately manual — this agent never calls ``vcs.merge``.
"""

from __future__ import annotations

import re
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
from virtual_dev.domain.models.merge_request import ReviewComment
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
    r"\b(change|fix|please\s+(update|change|fix|rename|remove|add)|needs? change|rework|wrong|rewrite|should (not )?be|don't|do not)\b",
    re.IGNORECASE,
)


def classify_comment(body: str) -> CommentClass:
    """Classify a single comment body.

    Lightweight heuristics; Phase 3 keeps the logic in code rather than
    LLM-classify to avoid round-tripping every comment through Claude.
    """
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
            await self._handle_human_comment(row, comment, klass)
            if self._message_bus is not None:
                await self._message_bus.publish(AgentMessage(
                    id=f"mr-comment-{comment.id}",
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

        # Approvals threshold check.
        required = self._config.agents.review_policy.required_approvals
        approvals_required = approvals.required if approvals.required > 0 else required
        if (
            approvals.count >= approvals_required
            and row.approvals_count < approvals_required
        ):
            logger.info(
                "Reviewer: {}!{} reached {} approvals; pinging to merge",
                row.repo_key, row.iid, approvals.count,
            )
            await self._notify_ready_to_merge(row)
            stats.approvals_sent += 1
            if self._message_bus is not None:
                await self._message_bus.publish(AgentMessage(
                    id=f"mr-approved-{row.repo_key}-{row.iid}",
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
            new_last_seen=(new_comments[-1].id if new_comments else None),
            approvals_count=approvals.count,
            approvals_required=approvals_required,
            touched=touched,
            escalated_this_tick=escalated_this_tick,
            now=now,
        )

    async def _handle_human_comment(
        self,
        row: MergeRequestRow,
        comment: ReviewComment,
        klass: CommentClass,
    ) -> None:
        """Relay interesting comments to Mattermost.

        Approvals are inferred from the approvals endpoint, so for those we
        do nothing here. Questions and change-requests get a heads-up ping
        so humans notice; chatter is ignored.
        """
        if klass in (CommentClass.APPROVAL_HINT, CommentClass.CHATTER):
            return
        channel_id = self._team_channel_for(row.repo_key)
        body = _render_comment_ping(row, comment, klass)
        if channel_id:
            await self._communicator.send_channel(channel_id, body)
            return
        escalation_user = await self._resolve_escalation_user()
        if escalation_user:
            await self._communicator.send_dm(escalation_user, body)
            return
        logger.info(
            "Reviewer: no channel / escalation target — comment on {}!{} not relayed",
            row.repo_key, row.iid,
        )

    async def _notify_ready_to_merge(self, row: MergeRequestRow) -> None:
        channel_id = self._team_channel_for(row.repo_key)
        msg = (
            f"[virtual-dev] MR `{row.repo_key}!{row.iid}` has collected enough approvals. "
            f"Please merge: {row.web_url}"
        )
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
                or now - _aware(row.last_escalation_at) >= timedelta(hours=policy.escalate_after_hours)
            ):
                user = await self._resolve_escalation_user()
                if user:
                    text = (
                        f"[virtual-dev] MR `{row.repo_key}!{row.iid}` has had no reviewer activity "
                        f"for {int(idle.total_seconds() / 3600)}h. Please chase or reassign: {row.web_url}"
                    )
                    await self._communicator.send_dm(user, text)
                    stats.escalations_sent += 1
                    if self._message_bus is not None:
                        await self._message_bus.publish(AgentMessage(
                            id=f"mr-stuck-{row.repo_key}-{row.iid}-{int(now.timestamp())}",
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
                text = (
                    f"[virtual-dev] MR `{row.repo_key}!{row.iid}` is waiting for a review "
                    f"({int(idle.total_seconds() / 3600)}h idle). {row.web_url}"
                )
                if channel_id:
                    await self._communicator.send_channel(channel_id, text)
                    stats.pings_sent += 1
                return True

        return False

    # --- Helpers ---

    def _new_comments(
        self, row: MergeRequestRow, comments: list[ReviewComment],
    ) -> list[ReviewComment]:
        """Return comments the Reviewer hasn't seen yet.

        We do **not** filter by MR author: under the current setup the
        GitLab token belongs to a single human, so MR author == reviewer ==
        everyone. Filtering would suppress every comment during smoke
        tests. Once a separate bot account exists, set
        ``bot_username`` in the constructor — then the bot's own
        ``reply_to_comment`` output won't round-trip back in.
        """
        filtered: list[ReviewComment] = []
        seen_cutoff = row.last_seen_comment_id or ""
        passed_cutoff = not seen_cutoff
        for comment in comments:
            # API returns oldest first; advance until we pass last_seen.
            if not passed_cutoff:
                if comment.id == seen_cutoff:
                    passed_cutoff = True
                continue
            if self._is_bot_author(comment.author_username):
                continue
            filtered.append(comment)
        return filtered

    def _is_bot_author(self, username: str) -> bool:
        """True iff the comment is from an explicit ``bot_username``.

        Without a dedicated bot GitLab account (current state), this is
        always False and we process every comment — including our own
        test comments.
        """
        if self._bot_username and username.lower() == self._bot_username.lower():
            return True
        return False

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
        new_last_seen: str | None,
        approvals_count: int,
        approvals_required: int,
        touched: bool,
        escalated_this_tick: bool,
        now: datetime,
    ) -> None:
        async with session_scope(self._session_factory) as session:
            row = (await session.execute(
                select(MergeRequestRow).where(MergeRequestRow.id == row_id)
            )).scalar_one_or_none()
            if row is None:
                return
            if new_last_seen:
                row.last_seen_comment_id = new_last_seen
            row.approvals_count = approvals_count
            row.approvals_required = approvals_required
            if touched:
                row.last_activity_at = now
                row.ping_reviewers_at = None
                row.last_escalation_at = None
            if escalated_this_tick:
                row.last_escalation_at = now


def _render_comment_ping(
    row: MergeRequestRow, comment: ReviewComment, klass: CommentClass,
) -> str:
    excerpt = comment.body.strip()
    if len(excerpt) > 400:
        excerpt = excerpt[:400] + "…"
    label = {
        CommentClass.QUESTION: "*question* on",
        CommentClass.CHANGE_REQUEST: "*change request* on",
        CommentClass.CHATTER: "comment on",
        CommentClass.APPROVAL_HINT: "approval on",
    }[klass]
    return (
        f"[virtual-dev] @{comment.author_username} left a {label} MR "
        f"`{row.repo_key}!{row.iid}`:\n> {excerpt}\n\n{row.web_url}"
    )


def _aware(dt: datetime | None) -> datetime:
    """Normalize possibly-naive SQLite datetimes to UTC-aware ones."""
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


__all__ = ["ReviewerAgent", "ReviewerTickStats", "CommentClass", "classify_comment"]
