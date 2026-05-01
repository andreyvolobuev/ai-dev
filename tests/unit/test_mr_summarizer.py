"""LLM-backed summarizer used by review_ping.

The 'please review' chat ping used to read just the MR title:
``Ребята, приглашаю на МР: DM-3343: Добавила валидацию этапов``.
Operators wanted a one-liner about WHAT was done, not just the
ticket name. The summarizer compresses the bot-authored MR
description into 1-2 first-person feminine fragments that slot
into the ``в котором я {summary}`` template.

Tests use a stub LlmPort so they're deterministic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from virtual_dev.application.services.mr_summarizer import MrSummarizer
from virtual_dev.domain.ports.llm import LlmMessage, LlmPort, LlmResponse


class _StubLlm(LlmPort):
    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[tuple[list[LlmMessage], str, str | None]] = []

    async def complete(
        self,
        messages: list[LlmMessage],
        *,
        model: str,
        system: str | None = None,
    ) -> LlmResponse:
        self.calls.append((messages, model, system))
        return LlmResponse(
            text=self._reply, input_tokens=20, output_tokens=10,
            stop_reason="end_turn", model=model,
        )

    def stream(  # pragma: no cover  — unused
        self, messages: list[LlmMessage], *, model: str, system: str | None = None,
    ) -> AsyncIterator[str]:
        async def _empty() -> AsyncIterator[str]:
            if False:
                yield ""
        return _empty()


@pytest.mark.asyncio
async def test_summarizer_returns_stripped_llm_text() -> None:
    """Happy path — model returns 1-2 feminine sentences, summarizer
    strips surrounding whitespace and returns as-is."""
    llm = _StubLlm("  добавила валидацию этапов и покрыла её тестами.  ")
    summarizer = MrSummarizer(llm=llm, model="claude-haiku-4-5")
    out = await summarizer.summarize(
        title="DM-3343: Add stage validation",
        description="Я добавила валидатор...",
    )
    assert out == "добавила валидацию этапов и покрыла её тестами."


@pytest.mark.asyncio
async def test_summarizer_uses_configured_model() -> None:
    llm = _StubLlm("сделала X")
    summarizer = MrSummarizer(llm=llm, model="claude-haiku-4-5")
    await summarizer.summarize(title="t", description="d")
    assert llm.calls[0][1] == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_summarizer_short_circuits_empty_body() -> None:
    """Empty title + empty description — no Haiku call, return ''."""
    llm = _StubLlm("ignored")
    summarizer = MrSummarizer(llm=llm, model="x")
    assert await summarizer.summarize(title="", description="") == ""
    assert await summarizer.summarize(title="   ", description="\n") == ""
    assert llm.calls == []


@pytest.mark.asyncio
async def test_summarizer_passes_title_and_description_to_llm() -> None:
    """Both title and description must reach the model verbatim — the
    title alone is too thin (often just the ticket key + truncated
    summary), the description is where the bot wrote what it did."""
    llm = _StubLlm("сделала")
    summarizer = MrSummarizer(llm=llm, model="x")
    await summarizer.summarize(
        title="DM-1: Add health endpoint",
        description="Добавила /healthz и тест.",
    )
    user_content = llm.calls[0][0][0].content
    assert "DM-1: Add health endpoint" in user_content
    assert "Добавила /healthz и тест." in user_content


def test_summarizer_system_prompt_pins_feminine_voice() -> None:
    """The persona is female (см. config/prompts/dev.md). System prompt
    must explicitly require feminine past-tense verbs so Haiku doesn't
    fall back to masculine forms — a single «сделал» breaks the
    persona in the chat ping."""
    from virtual_dev.application.services.mr_summarizer import (
        _SYSTEM_PROMPT,
    )
    lower = _SYSTEM_PROMPT.lower()
    assert "feminine" in lower or "женск" in lower or "сделала" in lower
    # 1-2 sentences hard cap is the user-stated requirement
    assert "two" in lower or "1" in lower or "2" in lower or "две" in lower


@pytest.mark.asyncio
async def test_summarizer_truncates_runaway_output() -> None:
    """Defence in depth: if the model ignores the 1-2-sentence rule
    and dumps a wall of text, don't post it in chat. Cap output to a
    soft length so the ping stays scannable."""
    long_text = "сделала первое предложение. " + ("ещё одно предложение. " * 100)
    llm = _StubLlm(long_text)
    summarizer = MrSummarizer(llm=llm, model="x")
    out = await summarizer.summarize(title="t", description="d")
    assert len(out) <= 320, f"output too long for a chat ping: {len(out)}"
    # First sentence (the most important one) must survive the trim.
    assert "сделала первое предложение" in out
