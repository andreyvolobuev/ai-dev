"""AnswerClassifier — verifies prompt rendering + structured-output parsing.

The Claude Agent SDK is stubbed; we override ``_call_model`` to feed
canned ``submit_classification`` payloads.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from virtual_dev.application.agents.answer_classifier import AnswerClassifier
from virtual_dev.application.services import InjectionFilter, PromptsLoader
from virtual_dev.domain.models.clarification import (
    Classification,
    OutOfScopeKind,
)
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


class _PreseedClassifier(AnswerClassifier):
    """AnswerClassifier with ``_call_model`` overridden to return canned data."""

    def __init__(self, *args: Any, captured: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._captured = captured

    async def _call_model(self, prompt: str) -> tuple[dict[str, Any], Any]:
        result = await self._code_agent.run_task(CodeAgentRequest(
            agent_key=self.agent_key, system_prompt="", user_prompt=prompt,
        ))
        return self._captured, result


def _config() -> AppConfig:
    return AppConfig(
        repositories=[RepositoryCfg(key="x", url="git@x:x.git")],
        agents=AgentsCfg(),
        mappings=MappingsCfg(),
        notifications=NotificationsCfg(),
    )


def _result(cost: float = 0.0001) -> CodeAgentResult:
    return CodeAgentResult(
        final_text="", turns=1, input_tokens=0, output_tokens=0,
        cost_usd=cost, stopped_reason="end_turn",
    )


def _classifier(
    captured: dict[str, Any], result: CodeAgentResult,
) -> _PreseedClassifier:
    return _PreseedClassifier(
        code_agent=_StubCodeAgent(result),
        config=_config(),
        prompts_loader=PromptsLoader("config/prompts"),
        injection_filter=InjectionFilter(),
        captured=captured,
    )


@pytest.mark.asyncio
async def test_direct_classification_extracts_payload() -> None:
    cls = _classifier(
        {
            "classification": "direct",
            "reasoning": "clear answer",
            "direct_answer_text": "UserAPI",
        },
        _result(),
    )
    out = await cls.classify(
        question_text="как называется ручка?",
        why_it_matters="без неё код не написать",
        coalesced_answer="UserAPI",
    )
    assert out.classification is Classification.DIRECT
    assert out.extracted["direct_answer_text"] == "UserAPI"


@pytest.mark.asyncio
async def test_redirect_classification_propagates_targets() -> None:
    cls = _classifier(
        {
            "classification": "redirect",
            "reasoning": "name + handle",
            "redirect_target_handle": "vasya",
            "redirect_target_name": "Вася Курочкин",
        },
        _result(),
    )
    out = await cls.classify(
        question_text="X?", why_it_matters="", coalesced_answer="спроси у Васи",
    )
    assert out.classification is Classification.REDIRECT
    assert out.extracted["redirect_target_handle"] == "vasya"


@pytest.mark.asyncio
async def test_counter_question_kind_propagated() -> None:
    cls = _classifier(
        {
            "classification": "counter_question",
            "reasoning": "needs more info",
            "counter_question_text": "какая ручка?",
            "counter_question_kind": "factual",
        },
        _result(),
    )
    out = await cls.classify(
        question_text="X?", why_it_matters="", coalesced_answer="какая?",
    )
    assert out.classification is Classification.COUNTER_QUESTION
    assert out.extracted["counter_question_kind"] == "factual"


@pytest.mark.asyncio
async def test_empty_capture_returns_out_of_scope_fallback() -> None:
    """When the LLM doesn't call submit_classification we fail-closed."""
    cls = _classifier({}, _result())
    out = await cls.classify(
        question_text="X?", why_it_matters="", coalesced_answer="",
    )
    assert out.classification is Classification.OUT_OF_SCOPE
    assert out.extracted["out_of_scope_kind"] == OutOfScopeKind.WRONG_PERSON.value


@pytest.mark.asyncio
async def test_invalid_classification_string_falls_back_to_out_of_scope() -> None:
    cls = _classifier(
        {"classification": "rubbish-not-a-real-enum", "reasoning": "?"},
        _result(),
    )
    out = await cls.classify(
        question_text="X?", why_it_matters="", coalesced_answer="text",
    )
    assert out.classification is Classification.OUT_OF_SCOPE


@pytest.mark.asyncio
async def test_handle_provided_path_for_asking_for_stakeholder() -> None:
    cls = _classifier(
        {
            "classification": "handle_provided",
            "reasoning": "they gave us the handle",
            "provided_handle": "vasya",
        },
        _result(),
    )
    out = await cls.classify(
        question_text="дай ник Васи",
        why_it_matters="",
        coalesced_answer="@vasya его ник",
        is_asking_for_stakeholder=True,
    )
    assert out.classification is Classification.HANDLE_PROVIDED
    assert out.extracted["provided_handle"] == "vasya"
