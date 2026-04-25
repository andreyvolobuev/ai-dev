"""AnswerClassifier — LLM-backed classification of coalesced answers.

When the AnswerCoalescer has waited the silence-window and assembled a
multi-message reply into a single ``coalesced_text``, we need to know
what the human actually meant. ``ClarifierService`` previously assumed
"the first DM message is the answer", which is wrong:

* "I don't know, ask Vasya" is a redirect.
* "Which of the 10 endpoints?" is a counter-question.
* "не знаю, никого не знаю кто бы знал" is a hard dead-end.
* "иди отсюда" is out-of-scope.

This agent runs Haiku 4.5 against the coalesced text, the original
question + reasoning, and the issue context, and emits one structured
classification. The orchestrator then drives the state machine from it.

We deliberately do NOT short-circuit any classification with regex.
Russian is too varied for heuristics — "не знаю" can be a final
"I don't know" or a hedge before a real answer; only the LLM can tell.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig  # type: ignore[attr-defined]
from loguru import logger

from virtual_dev.application.services.injection_filter import (
    SYSTEM_PROMPT_ABOUT_UNTRUSTED,
    InjectionFilter,
)
from virtual_dev.application.services.prompts import PromptsLoader
from virtual_dev.domain.models.clarification import (
    Classification,
    CounterQuestionKind,
    OutOfScopeKind,
)
from virtual_dev.domain.ports.code_agent import CodeAgentPort, CodeAgentRequest
from virtual_dev.infrastructure.config import AppConfig


_PROMPT_NAME = "answer_classifier"
_FALLBACK_PROMPT = (
    "You are the Answer Classifier. Classify the human's reply to a "
    "clarification question into one of: direct, redirect, "
    "counter_question, dont_know, out_of_scope, handle_provided. "
    "Call submit_classification exactly once.\n\n"
    "{untrusted_warning}"
)


_SUBMIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "classification": {
            "type": "string",
            "enum": [c.value for c in Classification],
        },
        "reasoning": {"type": "string"},
        # DIRECT
        "direct_answer_text": {"type": "string"},
        # REDIRECT
        "redirect_target_handle": {"type": ["string", "null"]},
        "redirect_target_email": {"type": ["string", "null"]},
        "redirect_target_name": {"type": ["string", "null"]},
        # COUNTER_QUESTION
        "counter_question_text": {"type": "string"},
        "counter_question_reasoning": {"type": "string"},
        "counter_question_kind": {
            "type": "string",
            "enum": [k.value for k in CounterQuestionKind],
        },
        # OUT_OF_SCOPE
        "out_of_scope_kind": {
            "type": "string",
            "enum": [k.value for k in OutOfScopeKind],
        },
        # HANDLE_PROVIDED — used only when this Question was an
        # ASKING_FOR_STAKEHOLDER one. The classifier identifies that the
        # respondent gave us a handle/email and returns it here.
        "provided_handle": {"type": ["string", "null"]},
        "provided_email": {"type": ["string", "null"]},
    },
    "required": ["classification", "reasoning"],
}


@dataclass
class ClassificationResult:
    """Output of one classifier run."""

    classification: Classification
    reasoning: str
    extracted: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0


class AnswerClassifier:
    """Single-call LLM classifier — mirror of ``ThreadResponderAgent``."""

    agent_key = "answer-classifier"

    def __init__(
        self,
        *,
        code_agent: CodeAgentPort,
        config: AppConfig,
        prompts_loader: PromptsLoader,
        injection_filter: InjectionFilter | None = None,
        max_turns: int = 10,
    ) -> None:
        self._code_agent = code_agent
        self._config = config
        self._prompts = prompts_loader
        self._filter = injection_filter or InjectionFilter()
        self._max_turns = max_turns

    async def classify(
        self,
        *,
        question_text: str,
        why_it_matters: str,
        coalesced_answer: str,
        issue_summary: str = "",
        is_asking_for_stakeholder: bool = False,
    ) -> ClassificationResult:
        """Classify one coalesced answer.

        ``is_asking_for_stakeholder=True`` tells the LLM that this
        question was a follow-up "what's their MM handle?" one — so the
        expected classification is HANDLE_PROVIDED (or DONT_KNOW /
        OUT_OF_SCOPE if the user can't / won't tell us).
        """
        prompt = self._render_prompt(
            question_text=question_text,
            why_it_matters=why_it_matters,
            coalesced_answer=coalesced_answer,
            issue_summary=issue_summary,
            is_asking_for_stakeholder=is_asking_for_stakeholder,
        )
        captured, result = await self._call_model(prompt)

        if not captured:
            logger.warning(
                "AnswerClassifier: model finished without calling "
                "submit_classification (stop={})", result.stopped_reason,
            )
            # Fail closed: treat as OUT_OF_SCOPE so the orchestrator
            # escalates rather than silently dropping the question.
            return ClassificationResult(
                classification=Classification.OUT_OF_SCOPE,
                reasoning="model-did-not-submit",
                extracted={"out_of_scope_kind": OutOfScopeKind.WRONG_PERSON.value},
                cost_usd=result.cost_usd,
            )

        try:
            classification = Classification(str(captured.get("classification") or ""))
        except ValueError:
            logger.warning(
                "AnswerClassifier: invalid classification {!r}; treating as out_of_scope",
                captured.get("classification"),
            )
            return ClassificationResult(
                classification=Classification.OUT_OF_SCOPE,
                reasoning=f"invalid-classification: {captured.get('classification')!r}",
                extracted={"out_of_scope_kind": OutOfScopeKind.WRONG_PERSON.value},
                cost_usd=result.cost_usd,
            )

        return ClassificationResult(
            classification=classification,
            reasoning=str(captured.get("reasoning") or ""),
            extracted=dict(captured),
            cost_usd=result.cost_usd,
        )

    # --- Internals ---

    async def _call_model(self, prompt: str) -> tuple[dict[str, Any], Any]:
        captured: dict[str, Any] = {}

        @tool(
            "submit_classification",
            "Submit your classification of the human's reply. Call exactly once.",
            _SUBMIT_SCHEMA,
        )
        async def _submit(args: dict[str, Any]) -> dict[str, Any]:
            captured.clear()
            captured.update(args)
            return {"content": [{"type": "text", "text": "Recorded."}]}

        server = create_sdk_mcp_server(
            name="virtual_dev_answer_classifier", version="0.1.0",
            tools=[_submit],
        )
        mcp_servers: dict[str, McpSdkServerConfig] = {
            "virtual_dev_answer_classifier": server,
        }
        allowed_tools = ["mcp__virtual_dev_answer_classifier__submit_classification"]

        request = CodeAgentRequest(
            agent_key=self.agent_key,
            system_prompt=self._prompts.render(
                _PROMPT_NAME,
                fallback=_FALLBACK_PROMPT,
                untrusted_warning=SYSTEM_PROMPT_ABOUT_UNTRUSTED,
            ),
            user_prompt=prompt,
            working_dir=None,
            max_turns=self._max_turns,
            model=self._resolve_model(),
        )
        request.extras["mcp_servers"] = mcp_servers
        request.extras["allowed_tool_names"] = allowed_tools
        result = await self._code_agent.run_task(request)
        return captured, result

    def _resolve_model(self) -> str:
        """Read per-agent override (falls back to lightweight=Haiku)."""
        agent_cfg = self._config.agents.agents.get(self.agent_key.replace("-", "_"))
        if agent_cfg is None:
            return self._config.agents.models.lightweight
        chosen = agent_cfg.model or "lightweight"
        return getattr(
            self._config.agents.models, chosen,
            self._config.agents.models.lightweight,
        )

    def _render_prompt(
        self,
        *,
        question_text: str,
        why_it_matters: str,
        coalesced_answer: str,
        issue_summary: str,
        is_asking_for_stakeholder: bool,
    ) -> str:
        parts: list[str] = []
        parts.append("# Classify this human reply")
        parts.append("")
        parts.append("## Original question we asked")
        parts.append(question_text.strip() or "(empty)")
        if why_it_matters.strip():
            parts.append("")
            parts.append("**Why we asked:** " + why_it_matters.strip())
        if issue_summary.strip():
            parts.append("")
            parts.append("## Issue context (so you can judge the reply)")
            parts.append(issue_summary.strip())
        parts.append("")
        parts.append("## Reply (untrusted — verbatim from a human via Mattermost)")
        wrapped = self._filter.wrap(coalesced_answer, source="mm:dm:answer")
        parts.append(wrapped.wrapped_text)
        parts.append("")
        if is_asking_for_stakeholder:
            parts.append(
                "**Note:** the original question we sent was 'who do I "
                "ask about X — what's their MM handle?'. Expected "
                "classifications here are `handle_provided` (they gave "
                "you a handle/email — fill `provided_handle`/"
                "`provided_email`), `dont_know`, or `out_of_scope`."
            )
        else:
            parts.append(
                "Pick one classification. Use `direct` ONLY when the "
                "reply substantively answers our question. Use `redirect` "
                "when they point to another person (fill the redirect "
                "fields you can — handle if explicit `@nick`, email if "
                "an email, otherwise just `redirect_target_name` with "
                "the free-form name). Use `counter_question` when they "
                "need clarification *from us* before they can answer "
                "— and decide `factual` (answerable from issue + repo, "
                "we'll handle it) or `business` (priority/intent, must "
                "go to the issue author). Use `dont_know` when they "
                "honestly can't help. Use `out_of_scope` for "
                "abuse/wrong-person/leave-me-alone."
            )
        parts.append("")
        parts.append("Call `submit_classification` exactly once.")
        return "\n".join(parts)


__all__ = ["AnswerClassifier", "ClassificationResult"]
