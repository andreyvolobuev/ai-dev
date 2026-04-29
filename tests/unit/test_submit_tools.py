"""Submit-tool double-call guards.

``submit_plan`` (analyst-side) refuses a second call within a single
run. ``submit_mr`` and ``submit_response`` had no such guard — a
hallucinating model that called the tool twice silently overwrote
``submit_capture``, surfacing as "weird MR title" or "wrong reply text"
downstream. These tests pin the guard contract for both.
"""

from __future__ import annotations

from typing import Any

import pytest

from virtual_dev.tools import ToolContext
from virtual_dev.tools.submit_mr import build as build_submit_mr
from virtual_dev.tools.submit_response import build as build_submit_response


def _make_ctx() -> ToolContext:
    """Tools require submit_capture + run_state in their dataclass — both
    bags are empty dicts at the start of a run."""
    return ToolContext(
        submit_capture={},
        run_state={},
        effects=[],
    )


async def _run(tool: Any, args: dict[str, Any]) -> dict[str, Any]:
    """SDK-built tools wrap the impl; the impl is at ``tool.handler``
    (see ``claude_agent_sdk.tool``). Call it directly."""
    # SDK exposes the underlying coroutine via .handler in our version;
    # fall back to invoking the tool object as a callable if needed.
    handler = getattr(tool, "handler", None) or tool
    return await handler(args)


@pytest.mark.asyncio
async def test_submit_mr_refuses_second_call() -> None:
    ctx = _make_ctx()
    tool = build_submit_mr(ctx)
    assert tool is not None

    first = await _run(tool, {
        "title": "Add foo", "description": "...", "status": "success",
    })
    assert first["content"][0]["text"]
    assert ctx.submit_capture == {
        "title": "Add foo", "description": "...", "status": "success",
    }

    # A second call must NOT overwrite — the runtime treats the first
    # capture as authoritative.
    second = await _run(tool, {
        "title": "Different MR", "description": "x", "status": "failed",
    })
    body = second["content"][0]["text"]
    assert "already_terminal" in body or "already" in body
    assert ctx.submit_capture == {
        "title": "Add foo", "description": "...", "status": "success",
    }


@pytest.mark.asyncio
async def test_submit_response_refuses_second_call() -> None:
    ctx = _make_ctx()
    tool = build_submit_response(ctx)
    assert tool is not None

    first = await _run(tool, {
        "action": "reply", "reply_text": "thanks", "reasoning": "polite",
    })
    assert first["content"][0]["text"]
    assert ctx.submit_capture["action"] == "reply"

    second = await _run(tool, {
        "action": "iterate",
        "iteration_feedback": "do X",
        "reasoning": "second thoughts",
    })
    body = second["content"][0]["text"]
    assert "already_terminal" in body or "already" in body
    # First capture preserved.
    assert ctx.submit_capture["action"] == "reply"


@pytest.mark.asyncio
async def test_submit_mr_marks_terminal_after_success() -> None:
    """The runtime relies on ``run_state['terminal']`` to know the
    agent's done; ensure the tool actually flips it."""
    ctx = _make_ctx()
    tool = build_submit_mr(ctx)
    assert tool is not None

    await _run(tool, {
        "title": "t", "description": "d", "status": "success",
    })
    assert ctx.run_state.get("terminal") is True


@pytest.mark.asyncio
async def test_submit_response_marks_terminal_after_success() -> None:
    ctx = _make_ctx()
    tool = build_submit_response(ctx)
    assert tool is not None

    await _run(tool, {"action": "ignore", "reasoning": "noise"})
    assert ctx.run_state.get("terminal") is True
