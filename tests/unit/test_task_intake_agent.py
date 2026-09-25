"""TaskIntakeAgent: решение по просьбе завести задачу.

Агент — одна LLM-итерация и единственный терминальный тул. Здесь
пинится то, что должно работать независимо от формулировок промпта:
тул реально зарегистрирован, тред попадает в промпт обёрнутым;
отсутствие submit не превращается в тихое создание задачи.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from virtual_dev.application.agents.task_intake import (
    IntakeAction,
    IntakeTicketState,
    TaskIntakeAgent,
)
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
    return AppConfig(repositories=[], agents=AgentsCfg(), mappings=MappingsCfg())


class _FakeCodeAgent(CodeAgentPort):
    def __init__(self, captured: dict[str, Any] | None = None) -> None:
        self.last_request: CodeAgentRequest | None = None
        self._captured = captured

    async def run_task(self, request: CodeAgentRequest) -> CodeAgentResult:
        self.last_request = request
        cap = request.extras.get("submit_capture") if request.extras else None
        if isinstance(cap, dict) and self._captured is not None:
            cap.update(self._captured)
        return CodeAgentResult(
            final_text="", turns=1, input_tokens=0, output_tokens=0,
            cost_usd=0.01, stopped_reason="end_turn",
        )

    def stream_task(self, request: CodeAgentRequest) -> AsyncIterator[str]:
        async def _empty() -> AsyncIterator[str]:
            if False:
                yield ""
        return _empty()


def _agent(fake: _FakeCodeAgent) -> TaskIntakeAgent:
    return TaskIntakeAgent(
        code_agent=fake,
        config=_cfg(),
        prompts_loader=PromptsLoader(Path("config") / "prompts"),
    )


def _post(text: str, *, root: str | None = None, author: str = "u1") -> ChatMessage:
    return ChatMessage(
        id="p-1", channel_id="chan-1", author_id=author, text=text,
        timestamp=datetime(2026, 9, 19, 10, 0, tzinfo=UTC),
        thread_root_id=root, trusted=False,
    )


async def test_intake_tool_surface_includes_submit_task_intake() -> None:
    """Регрессия: если ToolContext собран без run_state/submit_capture,
    build() тула вернёт None, тул тихо исчезнет из surface, и модель
    закончит ход текстом — задача не создастся; в логе появится
    невнятное предупреждение."""
    fake = _FakeCodeAgent()
    await _agent(fake).decide(post=_post("@ai-dev заведи задачу"), thread=[])

    assert fake.last_request is not None
    allowed = fake.last_request.extras.get("allowed_tool_names") or []
    assert any("submit_task_intake" in name for name in allowed)
    # Ходить в файловую систему интейку незачем — surface должен быть узким.
    assert not any(name in ("Read", "Glob", "Grep") for name in allowed)


async def test_create_decision_is_parsed() -> None:
    fake = _FakeCodeAgent(captured={
        "action": "create",
        "summary": "Сборка жёлтых карточек по Грузии",
        "description": "Нужно собрать жёлтые карточки по Грузии за сентябрь.",
        "assignee_hint": "",
        "reasoning": "прямая просьба завести задачу",
    })
    decision = await _agent(fake).decide(
        post=_post("@ai-dev заведи мне задачу на сборку жёлтых карточек по Грузии"),
        thread=[],
    )

    assert decision.action is IntakeAction.CREATE
    assert decision.summary == "Сборка жёлтых карточек по Грузии"
    assert "жёлтые карточки" in decision.description
    assert decision.assignee_hint == ""


async def test_thread_transcript_reaches_the_prompt_wrapped() -> None:
    """Сценарий 2: «прочитай тред». Транскрипт обязан дойти до модели и
    обязан быть обёрнут injection-фильтром — это чужой текст."""
    fake = _FakeCodeAgent(captured={"action": "create", "summary": "s",
                                    "description": "d", "reasoning": "r"})
    thread = [
        _post("надо собрать карточки по Грузии", author="u2"),
        _post("да, и по Армении тоже", author="u3"),
    ]
    await _agent(fake).decide(
        post=_post("@ai-dev прочитай тред и создай задачу", root="root-1"),
        thread=thread,
        permalink="https://mm.example/dm/pl/p-1",
    )

    assert fake.last_request is not None
    prompt = fake.last_request.user_prompt
    assert "по Армении тоже" in prompt
    assert "<untrusted_content" in prompt
    assert "https://mm.example/dm/pl/p-1" in prompt


async def test_update_decision_carries_changes_and_ticket_state() -> None:
    fake = _FakeCodeAgent(captured={
        "action": "update",
        "changes": {"assignee_hint": "Пётр Петров"},
        "reasoning": "просят переназначить",
    })
    decision = await _agent(fake).decide(
        post=_post("@ai-dev исполнителем поставь Петю", root="root-1"),
        thread=[],
        existing=IntakeTicketState(
            key="DM-4821", summary="Сборка карточек",
            assignee="ivan.ivanov", labels=["dmp-sup"],
        ),
    )

    assert decision.action is IntakeAction.UPDATE
    assert decision.changes == {"assignee_hint": "Пётр Петров"}
    assert fake.last_request is not None
    assert "DM-4821" in fake.last_request.user_prompt


async def test_busy_decision_uses_model_text() -> None:
    fake = _FakeCodeAgent(captured={
        "action": "busy",
        "reply_text": "Сейчас занята задачей, отвлечься не могу.",
        "reasoning": "это не просьба про тикет",
    })
    decision = await _agent(fake).decide(
        post=_post("@ai-dev что думаешь про новый парсер?"), thread=[],
    )

    assert decision.action is IntakeAction.BUSY
    assert "занята" in decision.reply_text


async def test_no_submit_degrades_to_busy_without_side_effects() -> None:
    """Модель не вызвала тул — интейк обязан ничего не создавать."""
    fake = _FakeCodeAgent(captured=None)
    decision = await _agent(fake).decide(post=_post("@ai-dev ..."), thread=[])

    assert decision.action is IntakeAction.BUSY
    assert decision.summary == ""
    assert decision.reasoning == "model-did-not-submit"


async def test_unknown_action_degrades_to_busy() -> None:
    fake = _FakeCodeAgent(captured={"action": "delete_everything", "reasoning": "x"})
    decision = await _agent(fake).decide(post=_post("@ai-dev ..."), thread=[])

    assert decision.action is IntakeAction.BUSY
