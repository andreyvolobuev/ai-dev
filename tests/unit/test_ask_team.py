"""ask_team (team-channel thread) and the submit_plan confirmation guard.

The guard exists because the analyst used to settle disputed design
points on its own, park them in ``risks`` and ship the plan — the team
only found out from a ready MR built on guesses.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from virtual_dev.application.agents.analyst import _proposal_confirmed, _team_thread
from virtual_dev.application.services.communicator import SendOutcome
from virtual_dev.domain.models.analyst_conversation import (
    ConversationStep,
    ConversationStepKind,
)
from virtual_dev.domain.models.chat import ChatMessage
from virtual_dev.tools import ToolContext
from virtual_dev.tools.ask_team import build as build_ask_team
from virtual_dev.tools.submit_plan import build as build_submit_plan


class _StubCommunicator:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str, str | None]] = []

    async def send_channel(
        self, channel_id: str, text: str, *, thread_root_id: str | None = None,
        reactive: bool = False,
    ) -> SendOutcome:
        self.posts.append((channel_id, text, thread_root_id))
        return SendOutcome(sent=True, message=ChatMessage(
            id=f"post-{len(self.posts)}", channel_id=channel_id, author_id="bot",
            text=text, timestamp=datetime.now(timezone.utc), trusted=True,
        ))


def _ctx(**run_state: Any) -> tuple[ToolContext, _StubCommunicator]:
    comm = _StubCommunicator()
    ctx = ToolContext(
        communicator=comm,  # type: ignore[arg-type]
        effects=[], submit_capture={},
        run_state={"ask_dispatched": False, "terminal": False, **run_state},
    )
    return ctx, comm


async def _run(tool: Any, args: dict[str, Any]) -> str:
    handler = getattr(tool, "handler", None) or tool
    return (await handler(args))["content"][0]["text"]


def _step(seq: int, kind: ConversationStepKind, **meta: Any) -> ConversationStep:
    return ConversationStep(
        id=seq, task_id=1, seq=seq, kind=kind,
        timestamp=datetime.now(timezone.utc), text="", metadata=meta,
    )


_PLAN = {"summary": "s", "steps": [], "risks": [], "status": "ready"}


@pytest.mark.asyncio
async def test_first_ask_opens_thread_with_ticket_link() -> None:
    ctx, comm = _ctx(team_channel_id="ch-team", team_thread=None, ticket_ref="[DM-1](u)")

    await _run(build_ask_team(ctx), {"message": "предлагаю X — ок?"})

    assert comm.posts == [("ch-team", "[DM-1](u)\n\nпредлагаю X — ок?", None)]
    effect = ctx.effects[0]
    assert effect.payload["audience"] == "team"
    assert effect.payload["asked_post_id"] == "post-1"
    assert effect.payload["target_user_id"] == ""


@pytest.mark.asyncio
async def test_follow_up_ask_replies_in_existing_thread() -> None:
    ctx, comm = _ctx(
        team_channel_id="ch-team",
        team_thread={"channel_id": "ch-team", "root_id": "root-1"},
        ticket_ref="[DM-1](u)",
    )

    await _run(build_ask_team(ctx), {"message": "итог: X. Все согласны?"})

    assert comm.posts == [("ch-team", "итог: X. Все согласны?", "root-1")]
    # Replies keep landing under the root, so that's what the inbox waits on.
    assert ctx.effects[0].payload["asked_post_id"] == "root-1"


@pytest.mark.asyncio
async def test_ask_team_without_channel_points_to_dm() -> None:
    ctx, comm = _ctx(team_channel_id=None, team_thread=None)

    body = await _run(build_ask_team(ctx), {"message": "?"})

    assert "no_team_channel" in body
    assert comm.posts == []
    assert not ctx.run_state["ask_dispatched"]


@pytest.mark.asyncio
async def test_plan_with_unconfirmed_decisions_is_refused() -> None:
    ctx, _ = _ctx(proposal_confirmed=False, confirm_below_confidence=0.9)

    body = await _run(build_submit_plan(ctx), {
        **_PLAN, "confidence": 0.95, "decisions": ["статусы armed/paused"],
    })

    assert "needs_confirmation" in body
    assert ctx.submit_capture == {}
    assert ctx.effects == []


@pytest.mark.asyncio
async def test_low_confidence_plan_is_refused() -> None:
    ctx, _ = _ctx(proposal_confirmed=False, confirm_below_confidence=0.9)

    body = await _run(build_submit_plan(ctx), {**_PLAN, "confidence": 0.8, "decisions": []})

    assert "needs_confirmation" in body


@pytest.mark.asyncio
async def test_confirmed_plan_with_decisions_is_recorded() -> None:
    ctx, _ = _ctx(proposal_confirmed=True, confirm_below_confidence=0.9)

    body = await _run(build_submit_plan(ctx), {
        **_PLAN, "confidence": 0.8, "decisions": ["команда согласовала X"],
    })

    assert '"recorded": true' in body
    assert ctx.submit_capture["decisions"] == ["команда согласовала X"]


@pytest.mark.asyncio
async def test_plan_straight_from_ticket_needs_no_confirmation() -> None:
    ctx, _ = _ctx(proposal_confirmed=False, confirm_below_confidence=0.9)

    body = await _run(build_submit_plan(ctx), {**_PLAN, "confidence": 0.95, "decisions": []})

    assert '"recorded": true' in body


def test_only_team_reply_confirms_when_team_channel_configured() -> None:
    dm_reply = [_step(1, ConversationStepKind.HUMAN_REPLIED, audience="dm")]
    team_reply = [_step(1, ConversationStepKind.HUMAN_REPLIED, audience="team")]

    assert not _proposal_confirmed(dm_reply, team_required=True)
    assert _proposal_confirmed(dm_reply, team_required=False)
    assert _proposal_confirmed(team_reply, team_required=True)


def test_team_thread_is_taken_from_latest_team_ask() -> None:
    history = [
        _step(1, ConversationStepKind.BOT_ASKED, audience="dm",
              channel_id="dm-1", asked_post_id="p1"),
        _step(2, ConversationStepKind.BOT_ASKED, audience="team",
              channel_id="ch-team", asked_post_id="root-1"),
    ]

    assert _team_thread(history) == {"channel_id": "ch-team", "root_id": "root-1"}
    assert _team_thread(history[:1]) is None


class _ThreadCommunicator(_StubCommunicator):
    """Team thread: the bot's root post plus whatever teammates wrote."""

    def __init__(self, thread: list[ChatMessage]) -> None:
        super().__init__()
        self.thread = thread

    async def read_thread(self, thread_root_id: str) -> list[ChatMessage]:
        return list(self.thread)

    async def username_for(self, user_id: str) -> str | None:
        return {"uid-a": "a.ivanova"}.get(user_id)

    async def sign_posts(self, posts: Any) -> str:
        from virtual_dev.application.services.communicator import CommunicatorService

        return await CommunicatorService.sign_posts(self, posts)  # type: ignore[arg-type]


def _post(post_id: str, text: str, *, trusted: bool = False) -> ChatMessage:
    return ChatMessage(
        id=post_id, channel_id="ch-team", author_id="bot" if trusted else "uid-a",
        text=text, timestamp=datetime.now(timezone.utc),
        thread_root_id="root-1", trusted=trusted,
    )


def _thread_ctx(thread: list[ChatMessage], seen: set[str]) -> tuple[ToolContext, _ThreadCommunicator]:
    comm = _ThreadCommunicator(thread)
    ctx = ToolContext(
        communicator=comm,  # type: ignore[arg-type]
        effects=[], submit_capture={},
        run_state={
            "ask_dispatched": False, "terminal": False,
            "team_channel_id": "ch-team",
            "team_thread": {"channel_id": "ch-team", "root_id": "root-1"},
            "team_seen_post_ids": seen,
            "proposal_confirmed": True, "confirm_below_confidence": 0.9,
        },
    )
    return ctx, comm


@pytest.mark.asyncio
async def test_submit_plan_shows_reply_written_mid_run_then_accepts() -> None:
    thread = [_post("root-1", "предлагаю X", trusted=True), _post("r-1", "ок"),
              _post("r-2", "и добавьте ещё алерт")]
    ctx, _ = _thread_ctx(thread, seen={"r-1"})
    plan = {**_PLAN, "confidence": 0.95, "decisions": []}

    first = await _run(build_submit_plan(ctx), plan)

    assert "new_team_replies" in first
    assert "@a.ivanova: и добавьте ещё алерт" in first
    assert ctx.submit_capture == {}
    assert ctx.effects[0].kind == "team_replies_seen"
    assert ctx.effects[0].payload["post_ids"] == ["r-2"]

    # Nothing new since — the plan goes through.
    second = await _run(build_submit_plan(ctx), plan)
    assert '"recorded": true' in second


@pytest.mark.asyncio
async def test_ask_team_shows_reply_written_mid_run_before_posting() -> None:
    thread = [_post("root-1", "предлагаю X", trusted=True), _post("r-2", "а как же Y?")]
    ctx, comm = _thread_ctx(thread, seen=set())

    first = await _run(build_ask_team(ctx), {"message": "итог: X"})

    assert "new_team_replies" in first
    assert comm.posts == []

    await _run(build_ask_team(ctx), {"message": "про Y: учла, итог X+Y"})
    assert comm.posts == [("ch-team", "про Y: учла, итог X+Y", "root-1")]
