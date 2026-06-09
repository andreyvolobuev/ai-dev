"""LLM-backed review-comment classifier (Stage 6).

The previous regex-based ``classify_comment`` violated the project rule
``feedback_no_regex_classification`` (Memory says: classify human text
via Haiku, never regex). This is the replacement.

Tests use a stub ``LlmPort`` so they're deterministic and don't burn
the live model.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from virtual_dev.application.agents.reviewer import CommentClass
from virtual_dev.application.services.review_comment_classifier import (
    ReviewCommentClassifier,
)
from virtual_dev.domain.ports.llm import LlmMessage, LlmPort, LlmResponse


class _StubLlm(LlmPort):
    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[tuple[list[LlmMessage], str]] = []

    async def complete(
        self,
        messages: list[LlmMessage],
        *,
        model: str,
        system: str | None = None,
    ) -> LlmResponse:
        self.calls.append((messages, model))
        return LlmResponse(
            text=self._reply, input_tokens=10, output_tokens=2,
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
async def test_classifier_uses_configured_haiku_model() -> None:
    llm = _StubLlm("change_request")
    classifier = ReviewCommentClassifier(llm=llm, model="claude-haiku-4-5")
    await classifier.classify("please rename this function")

    assert len(llm.calls) == 1
    _, model = llm.calls[0]
    assert model == "claude-haiku-4-5"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("change_request", CommentClass.CHANGE_REQUEST),
        ("CHANGE_REQUEST", CommentClass.CHANGE_REQUEST),
        ("question", CommentClass.QUESTION),
        ("  question\n", CommentClass.QUESTION),
        ("approval_hint", CommentClass.APPROVAL_HINT),
        ("chatter", CommentClass.CHATTER),
    ],
)
async def test_classifier_parses_well_formed_replies(
    reply: str, expected: CommentClass,
) -> None:
    classifier = ReviewCommentClassifier(llm=_StubLlm(reply), model="x")
    assert await classifier.classify("anything") == expected


@pytest.mark.asyncio
async def test_classifier_extracts_class_from_noisy_response() -> None:
    """The model sometimes prefixes/suffixes the answer (e.g. quoting,
    explaining); the classifier must still pull the class out."""
    classifier = ReviewCommentClassifier(
        llm=_StubLlm('The class is "change_request" because ...'),
        model="x",
    )
    assert await classifier.classify("X") == CommentClass.CHANGE_REQUEST


@pytest.mark.asyncio
async def test_classifier_falls_back_to_chatter_on_unparseable_response() -> None:
    """A misbehaving model returns gibberish — we must not crash; safer
    to default to chatter (no action) than to fabricate a response."""
    classifier = ReviewCommentClassifier(llm=_StubLlm("¯\\_(ツ)_/¯"), model="x")
    assert await classifier.classify("X") == CommentClass.CHATTER


@pytest.mark.asyncio
async def test_classifier_short_circuits_empty_body() -> None:
    """No reason to spend a Haiku call on whitespace."""
    llm = _StubLlm("question")
    classifier = ReviewCommentClassifier(llm=llm, model="x")
    assert await classifier.classify("") == CommentClass.CHATTER
    assert await classifier.classify("   ") == CommentClass.CHATTER
    assert llm.calls == []


@pytest.mark.asyncio
async def test_classifier_routes_coding_rule_through_change_request() -> None:
    """Coding-rule comments ("comments should explain WHY, not WHAT",
    "use double quotes", "always typecheck X") were silently bucketed
    as chatter because the previous schema only recognised "asks for a
    specific code change". The model can now reply with ``coding_rule``
    when the comment is a directive about how the codebase should be
    written; the parser maps it onto CHANGE_REQUEST so the downstream
    actionable-comment routing fires (ThreadResponder gets it, may
    iterate or push back via propose_alternative)."""
    classifier = ReviewCommentClassifier(llm=_StubLlm("coding_rule"), model="x")
    assert await classifier.classify("anything") == CommentClass.CHANGE_REQUEST


def test_classifier_prompt_documents_coding_rule_category() -> None:
    """The prompt must enumerate coding_rule and give Haiku at least
    one example so it picks the new label over the historical
    chatter fallback for convention/style directives."""
    from virtual_dev.application.services.review_comment_classifier import (
        _SYSTEM_PROMPT,
    )
    assert "coding_rule" in _SYSTEM_PROMPT, (
        "prompt must list coding_rule as a category the model can pick"
    )


@pytest.mark.asyncio
async def test_classifier_frames_comment_as_delimited_data() -> None:
    """Regression: the bare comment was sent as the user turn, so Haiku
    *answered* comment-shaped inputs instead of classifying them.

    Live repro: a bare reviewer question (see ``body`` below) made the
    model reply with an actual answer ("I need to clarify which
    dependency…") instead of a token; the non-token reply then silently
    degraded to chatter, and a real reviewer question was dropped with
    ``no action``. The user turn must
    present the comment as DELIMITED DATA and carry the classify
    instruction itself (not rely on the system prompt alone), so the
    model emits a category token rather than conversing with the comment.
    """
    llm = _StubLlm("question")
    classifier = ReviewCommentClassifier(llm=llm, model="x")
    body = "А для чего эта зависимость?"  # noqa: RUF001  — real reviewer comment, kept verbatim
    await classifier.classify(body)

    messages, _ = llm.calls[0]
    user_text = "\n".join(m.content for m in messages if m.role == "user")
    # Comment present verbatim, but wrapped so it can't be read as an
    # instruction to the assistant.
    assert body in user_text
    assert "<comment>" in user_text and "</comment>" in user_text
    # The classify directive rides in the user turn too.
    assert "classif" in user_text.lower()


@pytest.mark.asyncio
async def test_classifier_handles_russian_change_request() -> None:
    """Real motivation for the rewrite — the regex implementation never
    matched non-English change requests. Now we hand the body to the
    LLM verbatim and trust it; the test pins that the body reaches the
    LLM unchanged so a real Haiku can classify it correctly."""
    llm = _StubLlm("change_request")
    classifier = ReviewCommentClassifier(llm=llm, model="x")
    body = "исправь, пожалуйста, опечатку в названии функции"
    assert await classifier.classify(body) == CommentClass.CHANGE_REQUEST
    messages, _ = llm.calls[0]
    # Body lands in the user message verbatim — UTF-8 preserved, not
    # transliterated or stripped.
    assert any(body in m.content for m in messages)
