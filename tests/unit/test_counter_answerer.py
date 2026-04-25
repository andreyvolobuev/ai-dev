"""CounterQuestionAnswerer — happy path + low-confidence fallback."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from virtual_dev.application.agents.counter_answerer import CounterQuestionAnswerer
from virtual_dev.application.services import InjectionFilter, PromptsLoader
from virtual_dev.domain.ports.code_agent import (
    CodeAgentPort,
    CodeAgentRequest,
    CodeAgentResult,
)
from virtual_dev.infrastructure.config import (
    AgentsCfg,
    AppConfig,
    MappingsCfg,
    NotificationsCfg,
    RepositoryCfg,
)


class _StubCodeAgent(CodeAgentPort):
    def __init__(self, result: CodeAgentResult) -> None:
        self.result = result

    async def run_task(self, request: CodeAgentRequest) -> CodeAgentResult:
        return self.result

    def stream_task(self, request: CodeAgentRequest) -> AsyncIterator[str]:  # pragma: no cover
        raise NotImplementedError


class _PreseedAnswerer(CounterQuestionAnswerer):
    def __init__(self, *args: Any, captured: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._captured = captured

    async def _call_model(self, prompt: str, workspace: str | None) -> tuple[dict[str, Any], Any]:
        result = await self._code_agent.run_task(CodeAgentRequest(
            agent_key=self.agent_key, system_prompt="", user_prompt=prompt,
        ))
        return self._captured, result


def _answerer(captured: dict[str, Any]) -> _PreseedAnswerer:
    return _PreseedAnswerer(
        code_agent=_StubCodeAgent(CodeAgentResult(
            final_text="", turns=1, input_tokens=0, output_tokens=0,
            cost_usd=0.0001, stopped_reason="end_turn",
        )),
        config=AppConfig(
            repositories=[RepositoryCfg(key="x", url="git@x:x.git")],
            agents=AgentsCfg(), mappings=MappingsCfg(),
            notifications=NotificationsCfg(),
        ),
        prompts_loader=PromptsLoader("config/prompts"),
        researcher=None,
        injection_filter=InjectionFilter(),
        captured=captured,
    )


@pytest.mark.asyncio
async def test_factual_high_confidence_returns_answer() -> None:
    a = _answerer({
        "answer_text": "Имеется в виду /api/v2/users.",
        "confidence": 0.9,
        "escalate_to_reporter": False,
        "reasoning": "found in code",
    })
    out = await a.answer(
        original_question="Q",
        original_question_reasoning="",
        counter_question="какая ручка?",
        counter_question_reasoning="нужна конкретика",
        issue_summary="",
        repo_workspace=None,
    )
    assert out.confidence == 0.9
    assert out.escalate_to_reporter is False
    assert "/api/v2/users" in out.answer_text


@pytest.mark.asyncio
async def test_low_confidence_signals_fallback() -> None:
    a = _answerer({
        "answer_text": "",
        "confidence": 0.4,
        "escalate_to_reporter": True,
        "reasoning": "needs business decision",
    })
    out = await a.answer(
        original_question="Q",
        original_question_reasoning="",
        counter_question="что важнее?",
        counter_question_reasoning="",
        issue_summary="",
        repo_workspace=None,
    )
    assert out.escalate_to_reporter is True
    assert out.answer_text == ""


@pytest.mark.asyncio
async def test_no_capture_falls_back_to_escalate() -> None:
    a = _answerer({})
    out = await a.answer(
        original_question="Q", original_question_reasoning="",
        counter_question="?", counter_question_reasoning="",
        issue_summary="", repo_workspace=None,
    )
    assert out.escalate_to_reporter is True


@pytest.mark.asyncio
async def test_confidence_clamped_to_unit_interval() -> None:
    a = _answerer({
        "answer_text": "ok", "confidence": 5.0,
        "escalate_to_reporter": False, "reasoning": "x",
    })
    out = await a.answer(
        original_question="Q", original_question_reasoning="",
        counter_question="?", counter_question_reasoning="",
        issue_summary="", repo_workspace=None,
    )
    assert out.confidence == 1.0
