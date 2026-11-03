"""A chat thread rendered for a model prompt.

Timestamps are in the team's timezone: rendered in UTC, the bot quoted
«ок в 11:20» and «попытки 10:19–10:34» to people whose clocks said 14:20
(rainbow!1031).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from virtual_dev.domain.models.chat import ChatMessage

DEFAULT_TIMEZONE = "Europe/Moscow"


def local_time(moment: datetime | None, timezone_name: str = DEFAULT_TIMEZONE) -> str:
    """``2026-11-03 14:20`` in ``timezone_name``; ``""`` for None."""
    if moment is None:
        return ""
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo(DEFAULT_TIMEZONE)
    if moment.tzinfo is None:
        return moment.strftime("%Y-%m-%d %H:%M")
    return moment.astimezone(zone).strftime("%Y-%m-%d %H:%M")


def render_transcript(
    thread: Sequence[ChatMessage], timezone_name: str = DEFAULT_TIMEZONE,
) -> str:
    lines: list[str] = []
    for msg in thread:
        who = "bot" if msg.trusted else f"@{msg.author_id}"
        ts = local_time(msg.timestamp, timezone_name)
        lines.append(f"{who} [{ts}]\n{msg.text}".rstrip())
    return "\n\n".join(lines)


__all__ = ["DEFAULT_TIMEZONE", "local_time", "render_transcript"]
