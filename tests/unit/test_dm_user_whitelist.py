"""dm_user destination whitelist (Stage 8).

Without it, prompt-injection inside a ticket can steer the analyst into
DMing arbitrary people: "ask @ceo about the priority" → analyst runs
``find_chat_user_by_name("ceo")`` → ``dm_user`` happily sends. The
whitelist limits dm_user to: the ticket's reporter, the configured
escalation contact, and anyone the analyst has DMed in this
conversation already.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from virtual_dev.application.services.communicator import SendOutcome
from virtual_dev.domain.models.chat import ChatMessage
from virtual_dev.tools import ToolContext
from virtual_dev.tools.dm_user import build as build_dm_user


class _StubCommunicator:
    def __init__(self, *, resolve_to: str | None = "uid-x") -> None:
        self._resolve_to = resolve_to
        self.dms: list[tuple[str, str]] = []
        self.thread_calls: list[tuple[str, str, str | None, str | None]] = []

    async def resolve_user_id(
        self, *, username: str | None = None, email: str | None = None,
    ) -> str | None:
        return self._resolve_to

    async def send_dm(
        self,
        user_id: str,
        text: str,
        *,
        thread_channel_id: str | None = None,
        thread_root_id: str | None = None,
    ) -> SendOutcome:
        self.dms.append((user_id, text))
        self.thread_calls.append(
            (user_id, text, thread_channel_id, thread_root_id),
        )
        return SendOutcome(
            sent=True,
            message=ChatMessage(
                id="post-1", channel_id="ch-1", author_id="bot", text=text,
                timestamp=datetime.now(timezone.utc),
            ),
        )


def _make_ctx(
    *,
    allowed_handles: set[str] | None = None,
    allowed_emails: set[str] | None = None,
) -> tuple[ToolContext, _StubCommunicator]:
    comm = _StubCommunicator()
    ctx = ToolContext(
        communicator=comm,  # type: ignore[arg-type]
        effects=[],
        submit_capture={},
        run_state={
            "ask_dispatched": False,
            "terminal": False,
            "allowed_dm_handles": allowed_handles or set(),
            "allowed_dm_emails": allowed_emails or set(),
        },
    )
    return ctx, comm


async def _run(tool: Any, args: dict[str, Any]) -> dict[str, Any]:
    handler = getattr(tool, "handler", None) or tool
    return await handler(args)


@pytest.mark.asyncio
async def test_dm_user_rejects_handle_outside_whitelist() -> None:
    ctx, comm = _make_ctx(allowed_handles={"alice"})
    tool = build_dm_user(ctx)
    assert tool is not None

    result = await _run(tool, {"to_handle": "charlie", "message": "hi"})
    body = result["content"][0]["text"]
    assert '"sent": false' in body or '"sent":false' in body
    assert "not_allowed" in body or "not allowed" in body.lower()
    assert comm.dms == []


@pytest.mark.asyncio
async def test_dm_user_allows_reporter_handle() -> None:
    ctx, comm = _make_ctx(allowed_handles={"alice"})
    tool = build_dm_user(ctx)
    assert tool is not None

    result = await _run(tool, {"to_handle": "alice", "message": "hi"})
    body = result["content"][0]["text"]
    assert '"sent": true' in body or '"sent":true' in body
    assert comm.dms == [("uid-x", "hi")]


@pytest.mark.asyncio
async def test_dm_user_handle_match_is_case_insensitive() -> None:
    ctx, comm = _make_ctx(allowed_handles={"alice"})
    tool = build_dm_user(ctx)
    assert tool is not None

    result = await _run(tool, {"to_handle": "ALICE", "message": "hi"})
    body = result["content"][0]["text"]
    assert '"sent": true' in body or '"sent":true' in body
    assert len(comm.dms) == 1


@pytest.mark.asyncio
async def test_dm_user_strips_at_prefix_for_check() -> None:
    ctx, comm = _make_ctx(allowed_handles={"alice"})
    tool = build_dm_user(ctx)
    assert tool is not None

    result = await _run(tool, {"to_handle": "@alice", "message": "hi"})
    body = result["content"][0]["text"]
    assert '"sent": true' in body or '"sent":true' in body
    assert len(comm.dms) == 1


@pytest.mark.asyncio
async def test_dm_user_email_path_uses_email_whitelist() -> None:
    ctx, _comm = _make_ctx(allowed_emails={"alice@2gis.ru"})
    tool = build_dm_user(ctx)
    assert tool is not None

    # Allowed email passes.
    result = await _run(tool, {"to_email": "alice@2gis.ru", "message": "hi"})
    body = result["content"][0]["text"]
    assert '"sent": true' in body or '"sent":true' in body

    # An unlisted email is refused even though resolve would succeed.
    result = await _run(tool, {"to_email": "ceo@2gis.ru", "message": "x"})
    body = result["content"][0]["text"]
    assert '"sent": false' in body or '"sent":false' in body


@pytest.mark.asyncio
async def test_dm_user_threads_when_anchor_in_run_state() -> None:
    """When ``run_state['dm_threads']`` has an anchor for the resolved
    uid, ``dm_user`` plumbs it through ``send_dm`` so the message
    lands inside the existing DM thread."""
    comm = _StubCommunicator()
    ctx = ToolContext(
        communicator=comm,  # type: ignore[arg-type]
        effects=[],
        submit_capture={},
        run_state={
            "ask_dispatched": False,
            "terminal": False,
            "allowed_dm_handles": {"alice"},
            "allowed_dm_emails": set(),
            "dm_threads": {
                "uid-x": {"channel_id": "dm-1", "root_id": "bot-q1"},
            },
        },
    )
    tool = build_dm_user(ctx)
    assert tool is not None

    result = await _run(tool, {"to_handle": "alice", "message": "ещё"})
    body = result["content"][0]["text"]
    assert '"sent": true' in body or '"sent":true' in body
    assert comm.thread_calls == [("uid-x", "ещё", "dm-1", "bot-q1")]


@pytest.mark.asyncio
async def test_dm_user_top_level_when_no_anchor() -> None:
    """No entry in ``dm_threads`` for this uid → top-level DM (no
    thread params plumbed). Brand-new recipients hit this path."""
    comm = _StubCommunicator()
    ctx = ToolContext(
        communicator=comm,  # type: ignore[arg-type]
        effects=[],
        submit_capture={},
        run_state={
            "ask_dispatched": False,
            "terminal": False,
            "allowed_dm_handles": {"alice"},
            "allowed_dm_emails": set(),
            "dm_threads": {},
        },
    )
    tool = build_dm_user(ctx)
    assert tool is not None

    result = await _run(tool, {"to_handle": "alice", "message": "привет"})
    body = result["content"][0]["text"]
    assert '"sent": true' in body or '"sent":true' in body
    assert comm.thread_calls == [("uid-x", "привет", None, None)]


@pytest.mark.asyncio
async def test_dm_user_empty_whitelist_rejects_all() -> None:
    """Safe default: when no allowed targets are configured for a run,
    refuse every DM. Prevents accidental wide-open behaviour if a
    caller forgets to populate run_state."""
    ctx, comm = _make_ctx()
    tool = build_dm_user(ctx)
    assert tool is not None

    result = await _run(tool, {"to_handle": "alice", "message": "hi"})
    body = result["content"][0]["text"]
    assert '"sent": false' in body or '"sent":false' in body
    assert comm.dms == []
