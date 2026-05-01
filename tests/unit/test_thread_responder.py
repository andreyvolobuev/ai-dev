"""ThreadResponderAgent — tool surface + activity event tests.

Regression-prone area: ``submit_response.build()`` returns None when
``ToolContext.run_state`` is missing, which silently drops the
terminal tool from the MCP surface. The model then writes a plaintext
answer, ends its turn, the responder logs a WARNING and returns
IGNORE — chat goes silent and the operator has no UI signal.

Both surfaces are covered here:
* ``test_responder_tool_surface_includes_submit_response`` proves the
  ToolContext is wired so the tool actually registers.
* ``test_responder_emits_decision_activity_event`` proves every
  decision (including the "no-submit" failure) surfaces on the
  AgentTrace so the activity tab can render it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest

from virtual_dev.application.agents.thread_responder import (
    ResponderAction,
    ThreadResponderAgent,
)
from virtual_dev.application.services.agent_trace import AgentTrace
from virtual_dev.application.services.injection_filter import InjectionFilter
from virtual_dev.application.services.prompts import PromptsLoader
from virtual_dev.domain.models.chat import ChatMessage
from virtual_dev.domain.ports.code_agent import (
    CodeAgentPort,
    CodeAgentRequest,
    CodeAgentResult,
)
from virtual_dev.infrastructure.config.schema import (
    AgentsCfg,
    AppConfig,
    MappingsCfg,
)


def _cfg() -> AppConfig:
    return AppConfig(
        repositories=[],
        agents=AgentsCfg(),
        mappings=MappingsCfg(),
    )


class _FakeCodeAgent(CodeAgentPort):
    def __init__(self, captured_to_inject: dict[str, Any] | None = None) -> None:
        self.last_request: CodeAgentRequest | None = None
        self._captured_to_inject = captured_to_inject

    async def run_task(self, request: CodeAgentRequest) -> CodeAgentResult:
        self.last_request = request
        # If the test wired a submit_capture dict on extras, simulate
        # the model calling submit_response by populating it.
        cap = request.extras.get("submit_capture") if request.extras else None
        if isinstance(cap, dict) and self._captured_to_inject is not None:
            cap.update(self._captured_to_inject)
        return CodeAgentResult(
            final_text="", turns=1, input_tokens=0, output_tokens=0,
            cost_usd=0.01, stopped_reason="end_turn",
        )

    def stream_task(self, request: CodeAgentRequest) -> AsyncIterator[str]:  # pragma: no cover
        async def _empty() -> AsyncIterator[str]:
            if False:
                yield ""
        return _empty()


def _msg(*, id: str, author: str, text: str, root: str = "root-x") -> ChatMessage:
    return ChatMessage(
        id=id, channel_id="c", author_id=author, text=text,
        timestamp=datetime.now(timezone.utc), thread_root_id=root,
        trusted=False,
    )


@pytest.mark.asyncio
async def test_responder_tool_surface_includes_submit_response() -> None:
    """Regression: submit_response must register on the responder's
    MCP surface. If ToolContext is missing ``run_state``,
    ``submit_response.build()`` returns None and the tool silently
    drops — the model never sees it and ends the turn with plaintext.

    Asserts the kwarg the responder hands to the SDK includes the
    fully-qualified MCP tool name."""
    fake = _FakeCodeAgent()
    responder = ThreadResponderAgent(
        code_agent=fake,
        config=_cfg(),
        prompts_loader=PromptsLoader("/no-prompts-dir"),
        injection_filter=InjectionFilter(),
    )

    await responder.decide(
        mr_title="t", mr_description="d", mr_web_url="u",
        plan=None, thread=[], latest_reply=_msg(id="p", author="a", text="?"),
    )

    assert fake.last_request is not None
    allowed = fake.last_request.extras.get("allowed_tool_names") or []
    assert "mcp__virtual_dev_responder__submit_response" in allowed, (
        f"submit_response missing from MCP allow-list: {allowed}"
    )


@pytest.mark.asyncio
async def test_responder_emits_decision_activity_event_on_no_submit() -> None:
    """When the model skips submit_response (text-only turn) the
    responder currently returns IGNORE silently. Operators have no
    way to tell from the live UI that a comment was processed but
    the bot decided not to act — they assume it's broken. The
    activity tab must show every decision, including the failure
    case where reasoning='model-did-not-submit'."""
    fake = _FakeCodeAgent(captured_to_inject=None)   # no submit
    trace = AgentTrace()
    responder = ThreadResponderAgent(
        code_agent=fake,
        config=_cfg(),
        prompts_loader=PromptsLoader("/no-prompts-dir"),
        injection_filter=InjectionFilter(),
        trace=trace,
    )

    decision = await responder.decide(
        mr_title="DM-1: x", mr_description="", mr_web_url="https://gitlab/x/1",
        plan=None, thread=[], latest_reply=_msg(id="p", author="alice", text="?"),
    )

    assert decision.action == ResponderAction.IGNORE
    events = [e for e in list(trace._history) if e.type == "responder_decision"]
    assert len(events) == 1, (
        f"expected exactly one responder_decision event; "
        f"got types={[e.type for e in trace._history]}"
    )
    e = events[0]
    assert e.payload.get("action") == "ignore"
    assert e.payload.get("reasoning") == "model-did-not-submit"


@pytest.mark.asyncio
async def test_responder_emits_decision_activity_event_on_reply() -> None:
    """A normal reply decision must also surface on the activity tab
    so the operator can scan the responder's recent decisions
    (chat-only replies, iterations, push-backs) at a glance."""
    fake = _FakeCodeAgent(captured_to_inject={
        "action": "reply",
        "reply_text": "вот объяснение",
        "reasoning": "answers-the-question",
    })
    trace = AgentTrace()
    responder = ThreadResponderAgent(
        code_agent=fake,
        config=_cfg(),
        prompts_loader=PromptsLoader("/no-prompts-dir"),
        injection_filter=InjectionFilter(),
        trace=trace,
    )

    decision = await responder.decide(
        mr_title="DM-1: x", mr_description="", mr_web_url="https://gitlab/x/1",
        plan=None, thread=[], latest_reply=_msg(id="p", author="alice", text="?"),
    )

    assert decision.action == ResponderAction.REPLY
    events = [e for e in list(trace._history) if e.type == "responder_decision"]
    assert len(events) == 1
    e = events[0]
    assert e.payload.get("action") == "reply"
    assert e.payload.get("reply_text") == "вот объяснение"
    assert e.payload.get("reasoning") == "answers-the-question"
