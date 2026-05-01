# ruff: noqa: RUF001, RUF002
"""LLM-backed summarizer for the 'please review' chat ping.

The reviewer agent posts a one-off ping into the team channel when an
MR is ready for human review. Originally the ping was just the title:
``Ребята, приглашаю на МР: DM-3343: Добавила валидацию...`` — fine for
context-rich titles but useless for "DM-3343: implementation" style.

This service compresses the bot-authored MR description into a 1-2
sentence first-person feminine fragment that slots into the
``в котором я {summary}`` template. It runs only at review_ping time
(once per MR), so cost is bounded.
"""

from __future__ import annotations

import re

from virtual_dev.domain.ports.llm import LlmMessage, LlmPort

_SYSTEM_PROMPT = (
    "Ты — Аида Нейронова, AI-разработчица. Получаешь тело merge-request, "
    "который ты сама написала, и сжимаешь его в ОДНО ИЛИ ДВА коротких "
    "предложения, чтобы пригласить коллег на ревью в чате.\n\n"
    "Жёсткие правила:\n"
    "- Пиши в женском роде, прошедшем времени, от первого лица: "
    "«добавила», «исправила», «вынесла», «покрыла тестами» — НИКОГДА "
    "«добавил», «исправил».\n"
    "- Текст подставляется в шаблон «приглашаю на МР, в котором я "
    "{твой ответ}» — поэтому начинай с глагола в нижнем регистре, без "
    "точки в конце последнего фрагмента (или с точкой — на твоё "
    "усмотрение, но без вводных «я», «здесь я», и т.п.).\n"
    "- Максимум 2 предложения. Лучше одно ёмкое, чем два размытых.\n"
    "- Не повторяй ID тикета, не вставляй markdown-заголовки, не "
    "обрамляй кавычками. Только текст саммари.\n"
    "- Не упоминай тесты/линтеры/CI отдельно, если это просто "
    "сопровождающие шаги — фокус на сути изменений.\n\n"
    "Примеры хороших ответов:\n"
    "- «добавила валидацию этапов обработки и покрыла её тестами»\n"
    "- «починила N+1 в выгрузке заявок, вынесла префетч в репозиторий»\n"
    "- «обновила схему health-чека: теперь по подсистемам, не одной "
    "строкой»"
)

# Soft cap on the output length so a runaway model doesn't dump a wall
# of text into the team channel. Picked to fit a 2-sentence Russian
# ping comfortably; trim at the last sentence boundary if exceeded.
_MAX_LEN = 320


class MrSummarizer:
    """Single-method service. Holds an LlmPort + model name."""

    def __init__(self, *, llm: LlmPort, model: str) -> None:
        self._llm = llm
        self._model = model

    async def summarize(self, *, title: str, description: str) -> str:
        body = f"# {title}\n\n{description}".strip()
        if not body or body == "#":
            return ""
        # Empty title-and-description case: ``f"# \n\n"`` strips to "#"
        # which we already handle, but combinations with whitespace
        # only also need to short-circuit.
        if not title.strip() and not description.strip():
            return ""
        response = await self._llm.complete(
            messages=[LlmMessage(role="user", content=body)],
            model=self._model,
            system=_SYSTEM_PROMPT,
        )
        return _trim(response.text)


def _trim(raw: str) -> str:
    text = (raw or "").strip()
    if len(text) <= _MAX_LEN:
        return text
    # Cut at the last sentence boundary within the cap so we don't
    # truncate mid-word. Russian sentence enders: . ! ? + Russian
    # ellipsis "…".
    head = text[:_MAX_LEN]
    last_boundary = max(
        head.rfind("."), head.rfind("!"),
        head.rfind("?"), head.rfind("…"),
    )
    if last_boundary > 0:
        return head[:last_boundary + 1].rstrip()
    # No sentence boundary found — fall back to a hard cut on the last
    # whitespace so we don't break a word.
    last_space = head.rfind(" ")
    return (head[:last_space] if last_space > 0 else head).rstrip() + "…"


__all__ = ["MrSummarizer"]
# Keep ``re`` reachable for future regex-based post-processing (e.g.
# stripping markdown the model still sneaks in despite the prompt).
_ = re
