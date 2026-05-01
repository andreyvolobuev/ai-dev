"""LLM-backed classifier for code-review comments.

Replaces the previous regex implementation in
``application/agents/reviewer.py``: the rule
``feedback_no_regex_classification`` says human-text classification
must go through Haiku, never heuristics. Russian / mixed-language
review comments were the immediate motivator — the regex matched only
English keywords, so half of actionable comments were silently dropped
as "chatter".

Output is one of :class:`CommentClass`. The classifier short-circuits
empty bodies (no LLM call) and falls back to ``CHATTER`` if the model
returns something we can't map.
"""

from __future__ import annotations

import re

from virtual_dev.application.agents.reviewer import CommentClass
from virtual_dev.domain.ports.llm import LlmMessage, LlmPort

_SYSTEM_PROMPT = (
    "You classify code-review comments into exactly one of five "
    "categories. Reply with ONLY the category token (one word, "
    "snake_case), nothing else.\n\n"
    "Categories:\n"
    "- approval_hint: signals approval (LGTM, +1, ship it, looks good, "
    "одобряю, можно мерджить).\n"
    "- question: asks the author for clarification (in any language, "
    "with or without `?`).\n"
    "- change_request: asks for a SPECIFIC code change in this MR "
    "(rename foo to bar, fix the off-by-one in line 12, remove this "
    "import, исправь опечатку, поправь типы).\n"
    "- coding_rule: a directive about HOW the codebase should be "
    "written — a team convention or style rule the author should apply "
    "to this MR and going forward, rather than a single line edit. "
    "Examples: 'comments should explain WHY not WHAT', "
    "'use double quotes for strings', 'always typecheck X before Y', "
    "'не используем регулярки для классификации текста', "
    "'мы пишем комменты по принципу зачем тут этот код'.\n"
    "- chatter: noise that needs no action (thanks, nice work, fyi, "
    "спасибо, кстати, +кошки emoji).\n\n"
    "Comments may be in English, Russian, or mixed. Classify by intent, "
    "not by surface keywords. When a comment teaches a rule rather than "
    "asking for one specific edit, prefer coding_rule over chatter — "
    "it IS actionable, the dev should incorporate it.\n"
    "When a comment is borderline between change_request and "
    "coding_rule, prefer coding_rule (broader scope)."
)

# Map every spelling variant we'll accept from the model back to the
# enum. Anything not in here → CHATTER fallback. Note that
# ``coding_rule`` collapses onto CHANGE_REQUEST for the downstream
# routing (actionable vs not) — the distinction is for the prompt's
# benefit (Haiku has a clearer category boundary against chatter), not
# for the reviewer agent's branching.
_REPLY_TO_CLASS: dict[str, CommentClass] = {
    "approval_hint": CommentClass.APPROVAL_HINT,
    "question": CommentClass.QUESTION,
    "change_request": CommentClass.CHANGE_REQUEST,
    "coding_rule": CommentClass.CHANGE_REQUEST,
    "chatter": CommentClass.CHATTER,
}

_TOKEN_RE = re.compile(
    r"\b(approval_hint|question|change_request|coding_rule|chatter)\b",
    re.IGNORECASE,
)


class ReviewCommentClassifier:
    """Classifies a single review-comment body via a lightweight LLM."""

    def __init__(self, *, llm: LlmPort, model: str) -> None:
        self._llm = llm
        self._model = model

    async def classify(self, body: str) -> CommentClass:
        if not body.strip():
            return CommentClass.CHATTER
        response = await self._llm.complete(
            messages=[LlmMessage(role="user", content=body)],
            model=self._model,
            system=_SYSTEM_PROMPT,
        )
        return _parse(response.text)


def _parse(raw: str) -> CommentClass:
    """Pull the category token out of the model's reply.

    Direct map first (the prompt asks for one token, no decoration) —
    if that fails, scan for any of the four tokens anywhere in the
    text, since some models prefix or wrap their answer.
    """
    cleaned = (raw or "").strip().lower()
    direct = _REPLY_TO_CLASS.get(cleaned)
    if direct is not None:
        return direct
    match = _TOKEN_RE.search(cleaned)
    if match:
        return _REPLY_TO_CLASS[match.group(1)]
    return CommentClass.CHATTER
