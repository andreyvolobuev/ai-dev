"""ClarificationPlanner — LLM-backed next-step decider for one Goal.

The planner is invoked at every turn of a clarification dialogue:

* When a goal is first created (PENDING → first ASK).
* After the AnswerCoalescer flushes a coalesced reply (REPLANNING).
* After a ``wait_for_human`` deadline elapses.

It receives the goal description, why_it_matters, the contact hint
the analyst provided, the full append-only history, and the latest
fragments buffer. Tools available: Read/Glob/Grep + the Researcher
MCP + a planner-only ``lookup_mm_user``. The planner is encouraged to
self-research factual questions before DM-ing humans.

It outputs exactly one ``submit_decision`` call, a discriminated
union over five actions:

    ask              — DM a human (planner composes the message)
    achieve          — goal solved
    escalate_to_lead — give up, send full chain to escalation user
    abandon          — soft give-up (no escalation)
    wait_for_human   — defer; orchestrator schedules a re-poll

The planner replaces three former agents (AnswerClassifier,
CounterQuestionAnswerer, StakeholderResolver) — the hybrid 6-class
classification + state machine of Phase 3.8 didn't model "what is the
goal" and so lost it on multi-step dialogues.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig  # type: ignore[attr-defined]
from loguru import logger

from virtual_dev.application.services.agent_trace import AgentTrace
from virtual_dev.application.services.communicator import CommunicatorService
from virtual_dev.application.services.injection_filter import (
    SYSTEM_PROMPT_ABOUT_UNTRUSTED,
    InjectionFilter,
)
from virtual_dev.application.services.prompts import PromptsLoader
from virtual_dev.application.services.researcher import ResearcherToolkit
from virtual_dev.domain.models.clarification_goal import (
    ClarificationGoal,
    GoalStep,
    GoalStepKind,
    PlannerActionKind,
    PlannerDecision,
)
from virtual_dev.domain.ports.code_agent import CodeAgentPort, CodeAgentRequest
from virtual_dev.infrastructure.config import AppConfig

_PROMPT_NAME = "clarification_planner"
_FALLBACK_PROMPT = (
    "You are the Clarification Planner. Decide one next step for one "
    "ClarificationGoal: ask, achieve, escalate_to_lead, abandon, or "
    "wait_for_human. Call submit_decision exactly once.\n\n"
    "{untrusted_warning}"
)


_SUBMIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [k.value for k in PlannerActionKind],
        },
        "reasoning": {"type": "string"},

        # ASK
        "to_handle": {"type": ["string", "null"]},
        "to_email": {"type": ["string", "null"]},
        "message": {"type": ["string", "null"]},
        "dedupe_key": {"type": ["string", "null"]},

        # ACHIEVE
        "final_answer": {"type": ["string", "null"]},
        "confidence": {"type": ["number", "null"]},

        # ESCALATE / ABANDON
        "reason": {"type": ["string", "null"]},

        # WAIT
        "note": {"type": ["string", "null"]},
        "retry_after_minutes": {"type": ["integer", "null"]},
    },
    "required": ["action", "reasoning"],
}


@dataclass
class PlannerInput:
    goal: ClarificationGoal
    history: Sequence[GoalStep]
    latest_fragments: Sequence[str]
    issue_summary: str
    repo_workspace: str | None


class ClarificationPlanner:
    """LLM-backed step decider — one structured decision per call."""

    agent_key = "clarification-planner"

    def __init__(
        self,
        *,
        code_agent: CodeAgentPort,
        config: AppConfig,
        prompts_loader: PromptsLoader,
        communicator: CommunicatorService,
        researcher: ResearcherToolkit | None,
        injection_filter: InjectionFilter | None = None,
        trace: AgentTrace | None = None,
        max_turns: int | None = None,
    ) -> None:
        self._code_agent = code_agent
        self._config = config
        self._prompts = prompts_loader
        self._communicator = communicator
        self._researcher = researcher
        self._filter = injection_filter or InjectionFilter()
        self._trace = trace
        self._max_turns = max_turns or _planner_max_turns(config) or 20

    async def decide(self, inp: PlannerInput) -> PlannerDecision:
        prompt = self._render_prompt(inp)
        captured, result = await self._call_model(prompt, inp.repo_workspace)

        if not captured:
            logger.warning(
                "ClarificationPlanner: model finished without calling "
                "submit_decision (stop={})", result.stopped_reason,
            )
            # Fail-safe: escalate to lead. Better than silent freeze.
            return PlannerDecision(
                action=PlannerActionKind.ESCALATE_TO_LEAD,
                reasoning="model-did-not-submit",
                reason="planner did not produce a decision",
                cost_usd=result.cost_usd,
            )

        try:
            action = PlannerActionKind(str(captured.get("action") or "").lower())
        except ValueError:
            logger.warning(
                "ClarificationPlanner: invalid action {!r}; escalating",
                captured.get("action"),
            )
            return PlannerDecision(
                action=PlannerActionKind.ESCALATE_TO_LEAD,
                reasoning=f"invalid action {captured.get('action')!r}",
                reason="invalid planner output",
                cost_usd=result.cost_usd,
            )

        return PlannerDecision(
            action=action,
            reasoning=str(captured.get("reasoning") or ""),
            to_handle=_str_or_none(captured.get("to_handle")),
            to_email=_str_or_none(captured.get("to_email")),
            message=_str_or_none(captured.get("message")),
            dedupe_key=_str_or_none(captured.get("dedupe_key")),
            final_answer=_str_or_none(captured.get("final_answer")),
            confidence=_clamp_confidence(captured.get("confidence")),
            reason=str(captured.get("reason") or ""),
            note=str(captured.get("note") or ""),
            retry_after_minutes=_int_or_none(captured.get("retry_after_minutes")),
            cost_usd=result.cost_usd,
        )

    # ---------------------------------------------------------------- internals

    async def _call_model(
        self, prompt: str, workspace: str | None,
    ) -> tuple[dict[str, Any], Any]:
        captured: dict[str, Any] = {}

        @tool(
            "submit_decision",
            "Submit your single decision for this goal. Call exactly once.",
            _SUBMIT_SCHEMA,
        )
        async def _submit(args: dict[str, Any]) -> dict[str, Any]:
            captured.clear()
            captured.update(args)
            return {"content": [{"type": "text", "text": "Decision recorded."}]}

        submit_server = create_sdk_mcp_server(
            name="virtual_dev_planner_submit", version="0.1.0",
            tools=[_submit],
        )
        mcp_servers: dict[str, McpSdkServerConfig] = {
            "virtual_dev_planner_submit": submit_server,
        }
        allowed = ["mcp__virtual_dev_planner_submit__submit_decision"]

        # Researcher MCP — same tools the Analyst gets, but the
        # planner is told to use them for self-research before DM-ing.
        if self._researcher is not None:
            mcp_servers["virtual_dev_researcher"] = self._researcher.build_mcp_server()
            allowed.extend([
                "mcp__virtual_dev_researcher__search_code",
                "mcp__virtual_dev_researcher__read_file",
                "mcp__virtual_dev_researcher__kb_search",
                "mcp__virtual_dev_researcher__kb_fetch_page_by_url",
                "mcp__virtual_dev_researcher__search_mr_history",
            ])

        # MM lookup_mm_user. Imported lazily to avoid the
        # clarification.__init__ → orchestrator → planner → tools
        # circular when this module is loaded transitively.
        from virtual_dev.application.services.clarification.planner_tools import (
            build_planner_mcp_server,
        )

        lookup_server, lookup_tools = build_planner_mcp_server(self._communicator)
        mcp_servers["virtual_dev_planner_tools"] = lookup_server
        allowed.extend(lookup_tools)

        # File-system tools so factual questions about the repo can be
        # answered without DM-ing a human.
        allowed.extend(["Read", "Glob", "Grep"])

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
        request.extras["allowed_tool_names"] = allowed
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

    def _render_prompt(self, inp: PlannerInput) -> str:
        parts: list[str] = []
        parts.append("# Decide one next step for this clarification goal")
        parts.append("")
        parts.append("## Goal")
        parts.append(f"**What we need to learn:** {inp.goal.description}")
        if inp.goal.why_it_matters.strip():
            parts.append(f"**Why it matters:** {inp.goal.why_it_matters.strip()}")
        if inp.goal.initial_contact_hint.strip():
            parts.append(
                f"**Initial contact hint (from analyst):** "
                f"{inp.goal.initial_contact_hint.strip()}"
            )
        parts.append("")

        if inp.issue_summary.strip():
            parts.append("## Issue context")
            wrapped = self._filter.wrap(inp.issue_summary, source="issue:summary")
            parts.append(wrapped.wrapped_text)
            parts.append("")

        # History — append-only timeline of what's been tried.
        parts.append("## History so far (oldest first)")
        if not inp.history:
            parts.append("_(no steps yet — this is the first decision for this goal)_")
        else:
            for step in inp.history:
                parts.append(self._render_step(step))
        parts.append("")

        if inp.latest_fragments:
            parts.append("## Latest reply (untrusted, possibly multi-fragment)")
            joined = "\n\n".join(f.strip() for f in inp.latest_fragments if f.strip())
            wrapped_reply = self._filter.wrap(joined, source="mm:dm:reply")
            parts.append(wrapped_reply.wrapped_text)
            parts.append("")

        # Counters / budgets.
        parts.append("## Budgets")
        parts.append(
            f"- planner_calls_count: {inp.goal.planner_calls_count} "
            f"(circuit breaker after {_max_planner_calls(self._config)})"
        )
        parts.append("")

        parts.append(
            "Use the available tools (Read/Glob/Grep, Researcher MCP, "
            "lookup_mm_user) for self-research before deciding to DM a "
            "human. Then call `submit_decision` exactly once."
        )
        return "\n".join(parts)

    def _render_step(self, step: GoalStep) -> str:
        """Compact one-line + body rendering for the prompt."""
        recipient = ""
        if step.target_username:
            recipient = f" → @{step.target_username}"
        elif step.target_user_id:
            recipient = f" → {step.target_user_id}"
        head = f"**[{step.seq}] {step.kind.value}{recipient}** ({step.timestamp.strftime('%H:%M:%S') if step.timestamp else ''})"
        body = step.text.strip()
        if not body:
            return head
        # Wrap untrusted content from humans.
        if step.kind in (GoalStepKind.HUMAN_REPLIED, GoalStepKind.STALE_FRAGMENT):
            wrapped = self._filter.wrap(body, source=f"mm:dm:step:{step.seq}")
            return head + "\n" + wrapped.wrapped_text
        return head + "\n" + body


# ---------------------------------------------------------------- helpers


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clamp_confidence(value: Any) -> float:
    try:
        v = float(value or 0.0)
    except (TypeError, ValueError):
        v = 0.0
    return max(0.0, min(1.0, v))


def _planner_max_turns(config: AppConfig) -> int | None:
    cfg = config.agents.agents.get("clarification_planner")
    return cfg.max_iterations_per_task if cfg is not None else None


def _max_planner_calls(config: AppConfig) -> int:
    return config.agents.clarification.max_planner_calls_per_goal


__all__ = ["ClarificationPlanner", "PlannerInput"]
