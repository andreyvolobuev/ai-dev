"""Communicator service — read-only Phase-1 surface.

The Communicator is the only place chat-thread content is fetched and handed
to an LLM. It consolidates two responsibilities:

    1. Translate MM URLs (``.../pl/<post_id>``) into root-id lookups and
       fetch the full thread via ``ChatPort.read_thread``.
    2. Run each message through :class:`InjectionFilter` so that untrusted
       content is safe to paste into a prompt.

Phase 1 never sends messages; :meth:`summarise_thread_for_prompt` returns the
wrapped text and aggregated notes, which the Analyst splices into its LLM
prompt verbatim.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from loguru import logger

from virtual_dev.application.services.injection_filter import (
    InjectionFilter,
    WrappedUntrusted,
)
from virtual_dev.domain.ports.chat import ChatPort


@dataclass
class ThreadDigest:
    """Collected result of reading one MM thread."""

    source_url: str
    wrapped: WrappedUntrusted                 # ready-to-paste into LLM prompt
    message_count: int
    had_red_flags: bool


_POST_ID_IN_PATH_RE = re.compile(r"/pl/([A-Za-z0-9]+)")


class CommunicatorService:
    def __init__(self, chat: ChatPort | None, injection_filter: InjectionFilter) -> None:
        self._chat = chat
        self._filter = injection_filter

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
