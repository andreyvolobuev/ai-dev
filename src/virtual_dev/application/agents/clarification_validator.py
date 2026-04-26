"""ClarificationValidator — chain-aware answer validation.

After every SYNC tool result or coalesced human reply, the validator
gets:

* the task that was being worked on
* the FULL ancestor chain (root → … → current)
* the response text (tool payload or human reply)

It returns a structured verdict:

    {
      "resolves": [
        {"task_id": <int>, "final_answer": "...", "confidence": 0.0-1.0},
        ...
      ],
      "reasoning": "..."
    }

Why chain-aware: when we ask the issue reporter for Vasya's MM handle,
they might just paste the body example directly. The current task
("get Vasya's handle") doesn't get resolved, but the GRANDPARENT task
("get body example") does — and we should mark it solved and skip
the rest of the chain.

Validators are conservative: if the response is ambiguous (two
candidate handles, no obvious choice), they return zero resolves.
The orchestrator then sends the planner back for another tool pick.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig  # type: ignore[attr-defined]
from loguru import logger

from virtual_dev.application.services.agent_trace import AgentTrace
from virtual_dev.application.services.injection_filter import (
    SYSTEM_PROMPT_ABOUT_UNTRUSTED,
    InjectionFilter,
)
from virtual_dev.application.services.prompts import PromptsLoader
from virtual_dev.domain.models.clarification_task import (
    ClarificationTask,
)
from virtual_dev.domain.ports.code_agent import CodeAgentPort, CodeAgentRequest
from virtual_dev.infrastructure.config import AppConfig


_PROMPT_NAME = "clarification_validator"
_FALLBACK_PROMPT = (
    "You are the Clarification Validator. Decide which task(s) in the "
    "chain (if any) the supplied response resolves. Call submit_verdict "
    "exactly once.\n\n{untrusted_warning}"
)


_SUBMIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "resolves": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "final_answer": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["task_id", "final_answer", "confidence"],
            },
            "description": (
                "Tasks the response actually resolves. Empty array if "
                "the response is ambiguous, partial, or off-topic — "
                "the planner will pick another tool. Order doesn't "
                "matter; the orchestrator sorts by depth (deepest first)."
            ),
        },
        "reasoning": {"type": "string"},
    },
    "required": ["resolves", "reasoning"],
}


@dataclass
class ValidatorVerdict:
    """One per-task assertion in the validator's output."""

    task_id: int
    final_answer: str
    confidence: float


@dataclass
class ValidatorOutput:
    resolves: list[ValidatorVerdict] = field(default_factory=list)
    reasoning: str = ""
    cost_usd: float = 0.0


@dataclass
class ValidatorInput:
    task: ClarificationTask
    chain: Sequence[ClarificationTask]
    response_text: str
    response_source_label: str            # who/what produced the response
    response_source_class: str            # "mattermost" | "tool:..." | "human" | …
    issue_summary: str


class ClarificationValidator:
    """LLM-backed answer validator. One verdict per call."""

    agent_key = "clarification-validator"

    def __init__(
        self,
        *,
        code_agent: CodeAgentPort,
        config: AppConfig,
        prompts_loader: PromptsLoader,
        injection_filter: InjectionFilter | None = None,
        trace: AgentTrace | None = None,
        max_turns: int | None = None,
    ) -> None:
        self._code_agent = code_agent
        self._config = config
        self._prompts = prompts_loader
        self._filter = injection_filter or InjectionFilter()
        self._trace = trace
        self._max_turns = max_turns or 4   # validator is one-shot

    async def validate(self, inp: ValidatorInput) -> ValidatorOutput:
        prompt = self._render_prompt(inp)
        captured, result = await self._call_model(prompt)

        if not captured:
            logger.warning(
                "Validator: model finished without calling submit_verdict",
            )
            return ValidatorOutput(
                resolves=[], reasoning="model-did-not-submit",
                cost_usd=result.cost_usd,
            )

        raw_resolves = captured.get("resolves") or []
        verdicts: list[ValidatorVerdict] = []
        if isinstance(raw_resolves, list):
            for entry in raw_resolves:
                if not isinstance(entry, dict):
                    continue
                try:
                    task_id = int(entry.get("task_id"))
                except (TypeError, ValueError):
                    continue
                try:
                    confidence = float(entry.get("confidence") or 0.0)
                except (TypeError, ValueError):
                    confidence = 0.0
                final_answer = str(entry.get("final_answer") or "").strip()
                if not final_answer:
                    continue
                verdicts.append(ValidatorVerdict(
                    task_id=task_id,
                    final_answer=final_answer,
                    confidence=max(0.0, min(1.0, confidence)),
                ))
        return ValidatorOutput(
            resolves=verdicts,
            reasoning=str(captured.get("reasoning") or ""),
            cost_usd=result.cost_usd,
        )

    # ---------------------------------------------------------------- internals

    async def _call_model(
        self, prompt: str,
    ) -> tuple[dict[str, Any], Any]:
        captured: dict[str, Any] = {}

        @tool(
            "submit_verdict",
            "Submit your validation verdict. Call exactly once.",
            _SUBMIT_SCHEMA,
        )
        async def _submit(args: dict[str, Any]) -> dict[str, Any]:
            captured.clear()
            captured.update(args)
            return {"content": [{"type": "text", "text": "Verdict recorded."}]}

        submit_server = create_sdk_mcp_server(
            name="virtual_dev_validator_submit", version="0.1.0",
            tools=[_submit],
        )
        mcp_servers: dict[str, McpSdkServerConfig] = {
            "virtual_dev_validator_submit": submit_server,
        }
        allowed = ["mcp__virtual_dev_validator_submit__submit_verdict"]

        request = CodeAgentRequest(
            agent_key=self.agent_key,
            system_prompt=self._prompts.render(
                _PROMPT_NAME,
                fallback=_FALLBACK_PROMPT,
                untrusted_warning=SYSTEM_PROMPT_ABOUT_UNTRUSTED,
            ),
            user_prompt=prompt,
            max_turns=self._max_turns,
            model=self._resolve_model(),
        )
        request.extras["mcp_servers"] = mcp_servers
        request.extras["allowed_tool_names"] = allowed
        result = await self._code_agent.run_task(request)
        return captured, result

    def _resolve_model(self) -> str:
        agent_cfg = self._config.agents.agents.get(
            self.agent_key.replace("-", "_"),
        )
        if agent_cfg is None:
            # Validator is a small one-shot — Haiku is cheaper and faster.
            return self._config.agents.models.lightweight
        chosen = agent_cfg.model or "lightweight"
        return getattr(
            self._config.agents.models, chosen,
            self._config.agents.models.lightweight,
        )

    def _render_prompt(self, inp: ValidatorInput) -> str:
        parts: list[str] = []
        parts.append("# Validate which task(s) this response resolves")
        parts.append("")

        parts.append("## Chain (root first)")
        for t in inp.chain:
            mark = " [solved]" if t.is_solved else ""
            parts.append(
                f"- task #{t.id} (depth {t.depth}){mark}: "
                f"«{t.question.strip()}»"
                + (f" — info_source={t.info_source}"
                   if t.info_source else "")
            )
        parts.append("")

        parts.append("## Response under review")
        parts.append(
            f"Source: `{inp.response_source_label or '(unknown)'}` "
            f"(class={inp.response_source_class or '?'})"
        )
        wrapped = self._filter.wrap(
            inp.response_text, source="response",
        )
        parts.append(wrapped.wrapped_text)
        parts.append("")

        if inp.issue_summary.strip():
            parts.append("## Issue context")
            wrapped_issue = self._filter.wrap(
                inp.issue_summary, source="issue:summary",
            )
            parts.append(wrapped_issue.wrapped_text)
            parts.append("")

        parts.append(
            "Decide: which task in the chain (if any) does this "
            "response actually resolve? You may mark MULTIPLE tasks "
            "resolved at once if the response answers them all (e.g. "
            "respondent skipped levels and answered the root). Be "
            "conservative: ambiguous, partial, or off-topic responses "
            "→ empty resolves list. The orchestrator will then pick "
            "another tool. Call `submit_verdict` exactly once."
        )
        return "\n".join(parts)


__all__ = [
    "ClarificationValidator",
    "ValidatorInput",
    "ValidatorOutput",
    "ValidatorVerdict",
]
