"""Tests for Phase 3 Communicator write-side."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timezone

import pytest

from virtual_dev.application.services import CommunicatorService, InjectionFilter
from virtual_dev.application.services.communicator import (
    _is_within_working_hours,
)
from virtual_dev.domain.models.chat import ChatMessage, ChatUser
from virtual_dev.domain.ports.chat import ChatPort
from virtual_dev.infrastructure.config import WorkingHoursCfg


class _RecordingChat(ChatPort):
    """Fake ChatPort that records every send* call."""

    def __init__(self) -> None:
        self.sent_dms: list[tuple[str, str]] = []
        self.sent_channels: list[tuple[str, str, str | None]] = []

    async def read_thread(self, thread_root_id: str) -> Sequence[ChatMessage]:
        return []

    async def send_direct(self, user_id: str, text: str) -> ChatMessage:
        self.sent_dms.append((user_id, text))
        return _msg(text)

    async def send_to_channel(
        self, channel_id: str, text: str, thread_root_id: str | None = None
    ) -> ChatMessage:
        self.sent_channels.append((channel_id, text, thread_root_id))
        return _msg(text)

    async def find_user_by_email(self, email: str) -> ChatUser | None:
        return None

    async def find_user_by_username(self, username: str) -> ChatUser | None:
        if username == "alice":
            return ChatUser(id="uid-alice", username="alice")
        return None

    def subscribe(self) -> AsyncIterator[ChatMessage]:  # pragma: no cover
        raise NotImplementedError


def _msg(body: str) -> ChatMessage:
    return ChatMessage(
        id="m1", channel_id="c1", author_id="bot", text=body,
        timestamp=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_send_channel_delivers_inside_working_hours() -> None:
    chat = _RecordingChat()
    # Weekday 13:00 Moscow is inside working hours.
    wh = WorkingHoursCfg(timezone="Europe/Moscow", start_hour=10, end_hour=20)
    svc = CommunicatorService(
        chat, InjectionFilter(),
        working_hours=wh, rate_limit_per_hour=10,
        respect_working_hours=False,   # sent deterministically regardless of clock
    )

    outcome = await svc.send_channel("chan-1", "hi")
    assert outcome.sent is True
    assert chat.sent_channels == [("chan-1", "hi", None)]


@pytest.mark.asyncio
async def test_rate_limit_drops_message_after_quota() -> None:
    chat = _RecordingChat()
    svc = CommunicatorService(
        chat, InjectionFilter(),
        rate_limit_per_hour=2, respect_working_hours=False,
    )

    assert (await svc.send_channel("chan-A", "1")).sent is True
    assert (await svc.send_channel("chan-A", "2")).sent is True
    blocked = await svc.send_channel("chan-A", "3")
    assert blocked.sent is False
    assert blocked.skip_reason == "rate_limited"
    # Different target gets its own bucket.
    assert (await svc.send_channel("chan-B", "1")).sent is True


@pytest.mark.asyncio
async def test_working_hours_gate_blocks_outside_hours() -> None:
    chat = _RecordingChat()
    # Set a window that will never match the current time by picking
    # a 1-hour slot we can control via respect_working_hours=True and
    # an impossible range.
    wh = WorkingHoursCfg(timezone="UTC", start_hour=0, end_hour=0, weekdays_only=False)
    svc = CommunicatorService(
        chat, InjectionFilter(),
        working_hours=wh, respect_working_hours=True,
    )

    outcome = await svc.send_channel("chan", "hi")
    assert outcome.sent is False
    assert outcome.skip_reason == "outside_working_hours"
    assert chat.sent_channels == []


@pytest.mark.asyncio
async def test_resolve_user_id_prefers_username() -> None:
    chat = _RecordingChat()
    svc = CommunicatorService(chat, InjectionFilter(), respect_working_hours=False)
    assert await svc.resolve_user_id(username="alice") == "uid-alice"
    assert await svc.resolve_user_id(username="nobody") is None


def test_is_within_working_hours_weekday_window() -> None:
    wh = WorkingHoursCfg(timezone="Europe/Moscow", start_hour=10, end_hour=20, weekdays_only=True)
    # Monday 12:00 Moscow → within window.
    assert _is_within_working_hours(
        datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc), wh,    # 12:00 MSK Monday
    ) is True
    # Saturday 12:00 Moscow → out (weekdays only).
    assert _is_within_working_hours(
        datetime(2026, 4, 25, 9, 0, tzinfo=timezone.utc), wh,
    ) is False
    # Monday 23:00 Moscow → out of hours.
    assert _is_within_working_hours(
        datetime(2026, 4, 20, 20, 0, tzinfo=timezone.utc), wh,   # 23:00 MSK Monday
    ) is False


@pytest.mark.asyncio
async def test_send_drops_when_chat_not_configured() -> None:
    svc = CommunicatorService(None, InjectionFilter(), respect_working_hours=False)
    outcome = await svc.send_dm("uid", "hi")
    assert outcome.sent is False
    assert outcome.skip_reason == "chat_not_configured"
