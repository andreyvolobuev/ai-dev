"""Communicator service — the single choke-point for chat I/O.

Consolidates three responsibilities:

    1. **Read** — translate MM URLs (``.../pl/<post_id>``) into root-id
       lookups and fetch full threads via ``ChatPort.read_thread``. Run each
       message through :class:`InjectionFilter` so untrusted content is safe
       to paste into a prompt.
    2. **Write** — Phase 3, only Communicator sends messages so we can enforce
       rate limits + working-hours policy centrally. Bot authorship is
       implicit (the Mattermost token is the bot account).
    3. **User lookup** — resolve reviewer / team-lead MM handles from email
       or username so upstream agents don't touch ChatPort directly.

Phase 1 never sent messages. Phase 3 flips that on for ReviewerAgent /
DevOpsAgent pings + escalations.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from loguru import logger

from virtual_dev.application.services.injection_filter import (
    InjectionFilter,
    WrappedUntrusted,
)
from virtual_dev.domain.models.chat import ChatMessage
from virtual_dev.domain.ports.chat import ChatPort

if TYPE_CHECKING:
    from virtual_dev.infrastructure.config import WorkingHoursCfg


@dataclass
class ThreadDigest:
    """Collected result of reading one MM thread."""

    source_url: str
    wrapped: WrappedUntrusted                 # ready-to-paste into LLM prompt
    message_count: int
    had_red_flags: bool


_POST_ID_IN_PATH_RE = re.compile(r"/pl/([A-Za-z0-9]+)")


@dataclass
class SendOutcome:
    """Result of an attempted outbound message.

    ``sent`` is True when the message reached MM; False when it was
    suppressed by rate limit, working-hours policy, or missing chat adapter.
    ``skip_reason`` explains the suppression.
    """

    sent: bool
    message: ChatMessage | None = None
    skip_reason: str | None = None


@dataclass
class _RateBucket:
    """Sliding-window rate limiter state for one target (user/channel)."""

    window_seconds: int = 3600
    limit: int = 20
    timestamps: deque[datetime] = field(default_factory=deque)

    def try_consume(self, now: datetime) -> bool:
        cutoff = now.timestamp() - self.window_seconds
        while self.timestamps and self.timestamps[0].timestamp() < cutoff:
            self.timestamps.popleft()
        if len(self.timestamps) >= self.limit:
            return False
        self.timestamps.append(now)
        return True


class CommunicatorService:
    """Single gatekeeper for all bot-to-human chat I/O.

    Reads are cheap (happen on every Analyst run) and go through the
    InjectionFilter. Writes go through the rate limiter and working-hours
    gate so the bot cannot spam channels or DM people at 3am.
    """

    def __init__(
        self,
        chat: ChatPort | None,
        injection_filter: InjectionFilter,
        *,
        working_hours: "WorkingHoursCfg | None" = None,
        rate_limit_per_hour: int | None = None,
        respect_working_hours: bool = True,
    ) -> None:
        self._chat = chat
        self._filter = injection_filter
        if working_hours is None:
            from virtual_dev.infrastructure.config import WorkingHoursCfg as _WHC
            working_hours = _WHC()
        self._working_hours: Any = working_hours
        self._rate_limit = rate_limit_per_hour or 20
        self._respect_working_hours = respect_working_hours
        self._buckets: dict[str, _RateBucket] = {}

    # --- Writes (Phase 3) ---

    async def send_dm(self, user_id: str, text: str) -> SendOutcome:
        """DM the given user, subject to rate limit + working-hours policy."""
        return await self._send("dm", user_id, text, channel_id=None, thread_root_id=None)

    async def send_channel(
        self,
        channel_id: str,
        text: str,
        *,
        thread_root_id: str | None = None,
    ) -> SendOutcome:
        """Post to channel (optionally inside a thread)."""
        return await self._send("chan", channel_id, text, channel_id=channel_id, thread_root_id=thread_root_id)

    async def _send(
        self,
        kind: str,
        target_key: str,
        text: str,
        *,
        channel_id: str | None,
        thread_root_id: str | None,
    ) -> SendOutcome:
        if self._chat is None:
            logger.debug("Communicator: chat not configured; skipping {}", kind)
            return SendOutcome(sent=False, skip_reason="chat_not_configured")

        now = datetime.now(timezone.utc)
        if self._respect_working_hours and not _is_within_working_hours(now, self._working_hours):
            logger.info(
                "Communicator: outside working hours ({}), dropping {} to {!r}",
                self._working_hours.timezone, kind, target_key,
            )
            return SendOutcome(sent=False, skip_reason="outside_working_hours")

        bucket = self._buckets.setdefault(
            f"{kind}:{target_key}", _RateBucket(limit=self._rate_limit)
        )
        if not bucket.try_consume(now):
            logger.warning(
                "Communicator: rate limit hit for {} {!r} ({}/h); dropping",
                kind, target_key, self._rate_limit,
            )
            return SendOutcome(sent=False, skip_reason="rate_limited")

        try:
            if kind == "dm":
                message = await self._chat.send_direct(target_key, text)
            else:
                assert channel_id is not None
                message = await self._chat.send_to_channel(
                    channel_id, text, thread_root_id=thread_root_id,
                )
        except Exception as exc:
            # Keep it terse: MM connect errors produce 100+ line urllib3
            # tracebacks that drown the log. Debug builds can turn it back
            # on via LOG_LEVEL=DEBUG.
            logger.warning(
                "Communicator: {} send failed to {!r}: {}: {}",
                kind, target_key, type(exc).__name__, _short_cause(exc),
            )
            logger.debug("Communicator: full traceback", exc_info=True)
            return SendOutcome(sent=False, skip_reason="send_error")

        return SendOutcome(sent=True, message=message)

    async def add_reaction(self, post_id: str, emoji_name: str) -> None:
        """Pass-through to ChatPort.add_reaction.

        Reactions are technical idempotency markers, not user-facing
        messages — no rate limit, no working-hours gate.
        """
        if self._chat is None:
            return
        try:
            await self._chat.add_reaction(post_id, emoji_name)
        except Exception:
            logger.warning(
                "Communicator: add_reaction({!r}, {!r}) failed",
                post_id, emoji_name,
            )

    # --- User lookup (Phase 3 helper) ---

    async def resolve_user_id(
        self, *, username: str | None = None, email: str | None = None
    ) -> str | None:
        """Resolve an MM user id by username (preferred) or email.

        Returns ``None`` if chat is not configured or the user isn't found —
        ReviewerAgent / DevOpsAgent use this to look up reviewers and fall
        back to posting in a team channel when the DM target is unknown.
        """
        if self._chat is None:
            return None
        if username:
            user = await self._chat.find_user_by_username(username)
            if user is not None:
                return user.id
        if email:
            user = await self._chat.find_user_by_email(email)
            if user is not None:
                return user.id
        return None

    # --- Reads (Phase 1) ---

    async def digest_thread(self, url: str) -> ThreadDigest | None:
        """Fetch and wrap a single thread. Returns ``None`` if chat is not wired."""
        if self._chat is None:
            logger.debug("Chat adapter not configured; skipping thread {}", url)
            return None

        root_id = _extract_root_id(url)
        if root_id is None:
            logger.warning("Could not extract MM post/thread id from URL: {}", url)
            return None

        messages = await self._chat.read_thread(root_id)
        rendered = _render_messages(messages)
        wrapped = self._filter.wrap(rendered, source=f"mattermost:thread:{root_id}")

        return ThreadDigest(
            source_url=url,
            wrapped=wrapped,
            message_count=len(messages),
            had_red_flags=wrapped.had_red_flags,
        )

    async def digest_threads(self, urls: Sequence[str]) -> list[ThreadDigest]:
        digests: list[ThreadDigest] = []
        for url in urls:
            digest = await self.digest_thread(url)
            if digest is not None:
                digests.append(digest)
        return digests


def _extract_root_id(url: str) -> str | None:
    match = _POST_ID_IN_PATH_RE.search(url)
    if match:
        return match.group(1)
    query = parse_qs(urlparse(url).query)
    root = query.get("root") or query.get("root_id")
    if root:
        return root[0]
    return None


def _short_cause(exc: BaseException) -> str:
    """Walk the ``__cause__`` chain and return the innermost message.

    Makes DNS / connection failures readable: the top-level exception is
    usually ``requests.ConnectionError: HTTPSConnectionPool...`` and the
    useful root is the ``gaierror`` a few frames deep.
    """
    cur: BaseException | None = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if cur.__cause__ is None and cur.__context__ is None:
            break
        cur = cur.__cause__ or cur.__context__
    text = str(cur or exc)
    return text.splitlines()[0][:200] if text else type(exc).__name__


def _is_within_working_hours(now: datetime, cfg: "WorkingHoursCfg") -> bool:
    """Local time in ``cfg.timezone`` between start/end hours (weekdays only if set)."""
    try:
        tz = ZoneInfo(cfg.timezone)
    except Exception:
        logger.warning("Unknown timezone {!r}; treating as UTC", cfg.timezone)
        tz = timezone.utc
    local = now.astimezone(tz)
    if cfg.weekdays_only and local.weekday() >= 5:   # 5=Sat, 6=Sun
        return False
    return cfg.start_hour <= local.hour < cfg.end_hour


def _render_messages(messages: Sequence[object]) -> str:
    """Render a thread as ``@author [ts]\\nbody`` lines, oldest first.

    Kept loosely typed so we can accept both :class:`ChatMessage` and any
    test fake with the same shape.
    """
    lines: list[str] = []
    for msg in messages:
        author = getattr(msg, "author_id", "unknown")
        ts = getattr(msg, "timestamp", None)
        body = getattr(msg, "text", "")
        ts_str = ts.isoformat() if ts is not None else ""
        lines.append(f"@{author} [{ts_str}]\n{body}".rstrip())
    return "\n\n".join(lines)
