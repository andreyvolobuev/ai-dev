"""CounterQuestionAnswerer — bot self-answers factual counter-questions.

When the AnswerClassifier marks a reply as
``counter_question / counter_question_kind=factual`` (e.g. respondent
asked "which of the 10 endpoints?"), we don't bounce the question back
to the issue author. Instead we (the bot) compose an answer using the
issue context + read access to the repo, and post it in the same DM
thread so the original respondent can keep working on the real
question.

This agent runs Sonnet 4.5 with the same tool-set as the Analyst
(Read/Glob/Grep + the Researcher MCP) but a much narrower brief: "look
at the issue + counter-question, draft a 1-3 paragraph reply".

If the agent's own confidence is low, it sets
``escalate_to_reporter=true`` and the orchestrator fall-back-routes to
the Issue author instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig  # type: ignore[attr-defined]
from loguru import logger

from virtual_dev.application.services.injection_filter import (
    SYSTEM_PROMPT_ABOUT_UNTRUSTED,
    InjectionFilter,
)
from virtual_dev.application.services.prompts import PromptsLoader
from virtual_dev.application.services.researcher import ResearcherToolkit
from virtual_dev.domain.ports.code_agent import CodeAgentPort, CodeAgentRequest
from virtual_dev.infrastructure.config import AppConfig


_PROMPT_NAME = "counter_answerer"
_FALLBACK_PROMPT = (
    "You are the Counter-Question Answerer. Compose a short factual "
    "reply to the human's clarifying counter-question, using the issue "
    "context and the repo. Call submit_counter_answer exactly once.\n\n"
    "{untrusted_warning}"
)


_SUBMIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer_text": {"type": "string"},
        "confidence": {"type": "number"},
        "escalate_to_reporter": {"type": "boolean"},
        "reasoning": {"type": "string"},
    },
    "required": ["answer_text", "confidence", "escalate_to_reporter", "reasoning"],
}


@dataclass
class CounterAnswerResult:
    answer_text: str
    confidence: float
    escalate_to_reporter: bool
    reasoning: str
    cost_usd: float = 0.0


class CounterQuestionAnswerer:
    agent_key = "counter-answerer"

    def __init__(
        self,
        *,
        code_agent: CodeAgentPort,
        config: AppConfig,
        prompts_loader: PromptsLoader,
        researcher: ResearcherToolkit | None,
        injection_filter: InjectionFilter | None = None,
        max_turns: int | None = None,
    ) -> None:
        self._code_agent = code_agent
        self._config = config
        self._prompts = prompts_loader
        self._researcher = researcher
        self._filter = injection_filter or InjectionFilter()
        self._max_turns = (
            max_turns or _max_turns_from_config(config) or 20
        )

    async def answer(
        self,
        *,
        original_question: str,
        original_question_reasoning: str,
        counter_question: str,
        counter_question_reasoning: str,
        issue_summary: str,
        repo_workspace: str | None,
    ) -> CounterAnswerResult:
        prompt = self._render_prompt(
            original_question=original_question,
            original_question_reasoning=original_question_reasoning,
            counter_question=counter_question,
            counter_question_reasoning=counter_question_reasoning,
            issue_summary=issue_summary,
        )
        captured, result = await self._call_model(prompt, repo_workspace)

        if not captured:
            logger.warning(
                "CounterAnswerer: model finished without calling "
                "submit_counter_answer (stop={})", result.stopped_reason,
            )
            return CounterAnswerResult(
                answer_text="",
                confidence=0.0,
                escalate_to_reporter=True,
                reasoning="model-did-not-submit",
                cost_usd=result.cost_usd,
            )

        try:
            confidence = float(captured.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        return CounterAnswerResult(
            answer_text=str(captured.get("answer_text") or "").strip(),
            confidence=confidence,
            escalate_to_reporter=bool(captured.get("escalate_to_reporter")),
            reasoning=str(captured.get("reasoning") or ""),
            cost_usd=result.cost_usd,
        )

    # --- Internals ---

    async def _call_model(
        self, prompt: str, workspace: str | None,
    ) -> tuple[dict[str, Any], Any]:
        captured: dict[str, Any] = {}

        @tool(
            "submit_counter_answer",
            "Submit your draft answer to the counter-question. "
            "Call exactly once at the end.",
            _SUBMIT_SCHEMA,
        )
        async def _submit(args: dict[str, Any]) -> dict[str, Any]:
            captured.clear()
            captured.update(args)
            return {"content": [{"type": "text", "text": "Recorded."}]}

        server = create_sdk_mcp_server(
            name="virtual_dev_counter_answerer", version="0.1.0",
            tools=[_submit],
        )
        mcp_servers: dict[str, McpSdkServerConfig] = {
            "virtual_dev_counter_answerer": server,
        }
        allowed_tools = [
            "mcp__virtual_dev_counter_answerer__submit_counter_answer",
            "Read", "Glob", "Grep",
        ]
        if self._researcher is not None:
            mcp_servers["virtual_dev_researcher"] = self._researcher.build_mcp_server()
            allowed_tools.extend([
                "mcp__virtual_dev_researcher__search_code",
                "mcp__virtual_dev_researcher__read_file",
                "mcp__virtual_dev_researcher__kb_search",
                "mcp__virtual_dev_researcher__kb_fetch_page_by_url",
                "mcp__virtual_dev_researcher__search_mr_history",
            ])

        request = CodeAgentRequest(
            agent_key=self.agent_key,
            system_prompt=self._prompts.render(
                _PROMPT_NAME,
                fallback=_FALLBACK_PROMPT,
                untrusted_warning=SYSTEM_PROMPT_ABOUT_UNTRUSTED,
            ),
            user_prompt=prompt,
            working_dir=workspace,
            max_turns=self._max_turns,
            model=self._resolve_model(),
        )
        request.extras["mcp_servers"] = mcp_servers
        request.extras["allowed_tool_names"] = allowed_tools
        result = await self._code_agent.run_task(request)
        return captured, result

    def _resolve_model(self) -> str:
        agent_cfg = self._config.agents.agents.get(self.agent_key.replace("-", "_"))
        if agent_cfg is None:
            return self._config.agents.models.default
        chosen = agent_cfg.model or "default"
        return getattr(
            self._config.agents.models, chosen, self._config.agents.models.default,
        )

    def _render_prompt(
        self,
        *,
        original_question: str,
        original_question_reasoning: str,
        counter_question: str,
        counter_question_reasoning: str,
        issue_summary: str,
    ) -> str:
        parts: list[str] = []
        parts.append("# Compose a counter-answer")
        parts.append("")
        parts.append("## Original question we asked the human")
        parts.append(original_question.strip() or "(empty)")
        if original_question_reasoning.strip():
            parts.append("")
            parts.append("**Why we asked:** " + original_question_reasoning.strip())
        parts.append("")
        parts.append("## They replied with a counter-question (untrusted)")
        wrapped_q = self._filter.wrap(counter_question, source="mm:dm:counter")
        parts.append(wrapped_q.wrapped_text)
        if counter_question_reasoning.strip():
            parts.append("")
            parts.append(
                "Classifier extracted the reasoning: " + counter_question_reasoning.strip()
            )
        parts.append("")
        parts.append("## Issue summary (for context)")
        wrapped_summary = self._filter.wrap(issue_summary, source="issue:summary")
        parts.append(wrapped_summary.wrapped_text)
        parts.append("")
        parts.append(
            "Use Read/Glob/Grep + (if available) the researcher tools "
            "to find the answer in the issue + repo. Compose a SHORT "
            "(1–3 paragraph) reply that closes the counter-question so "
            "the human can keep working on our original question."
        )
        parts.append("")
        parts.append(
            "If you cannot answer with high confidence (you don't have "
            "enough context, or the answer requires business judgment "
            "rather than facts) — set `escalate_to_reporter=true`, "
            "leave `answer_text` empty, and the orchestrator will route "
            "the counter-question to the issue author."
        )
        parts.append("")
        parts.append(
            "Confidence rubric:\n"
            "- 0.9-1.0: facts directly visible in code/issue.\n"
            "- 0.7-0.9: inferred from documented patterns.\n"
            "- 0.5-0.7: educated guess; consider escalating.\n"
            "- < 0.5: don't answer; escalate."
        )
        parts.append("")
        parts.append("Call `submit_counter_answer` exactly once.")
        return "\n".join(parts)


def _max_turns_from_config(config: AppConfig) -> int | None:
    cfg = config.agents.agents.get("counter_answerer")
    return cfg.max_iterations_per_task if cfg is not None else None


__all__ = ["CounterQuestionAnswerer", "CounterAnswerResult"]
