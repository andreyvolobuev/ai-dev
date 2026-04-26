"""ClarificationAgent — single Claude-Code-like agent per task.

Replaces ``ClarificationToolPicker`` + ``ClarificationValidator`` from
Phase 4.5. Instead of one LLM picking a tool and a separate LLM
validating each result, one continuous-reasoning agent drives the
whole task:

    LLM thinks → calls a SYNC tool → reads result → thinks → calls
    another SYNC tool → reads result → … → either calls
    ``ask_mm_user`` (ASYNC, ends the turn until human replies) or
    ``submit_final_answer`` / ``escalate_to_lead`` / ``abandon``
    (terminal).

This mirrors how Claude Code itself works: one continuous chain of
thought, all tools (Read, Bash, Grep, MCP, …) equally available, the
LLM decides when it's done.

Persistence: each agent run is a one-shot session against the SDK
(stateless from the SDK's perspective). To preserve "continuous
reasoning" across human-reply latency, we re-build the user prompt
on every invocation with the **full step history** so the LLM sees
everything it has done and learned.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
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
from virtual_dev.domain.models.clarification_task import (
    ClarificationTask,
    TaskStep,
    TaskStepKind,
)
from virtual_dev.domain.ports.code_agent import CodeAgentPort, CodeAgentRequest
from virtual_dev.infrastructure.config import AppConfig


_PROMPT_NAME = "clarification_agent"
_FALLBACK_PROMPT = (
    "You are the Clarification Agent. Resolve one ClarificationTask "
    "by chaining tools. Stop when you call submit_final_answer / "
    "escalate_to_lead / abandon, or after ask_mm_user (which is async; "
    "you'll be re-invoked on human reply).\n\n{untrusted_warning}"
)


@dataclass
class AgentEffect:
    """One side-effect a tool produced during the agent run.

    The orchestrator inspects effects after the run to decide what to
    do next: solved → cascade, awaiting → return, escalated → lead-DM,
    etc. The agent also receives the effect as a tool result text so
    the LLM understands what its action did.
    """

    kind: str   # "ask_dispatched" | "final_answer" | "escalate" | "abandon"
    payload: dict[str, Any]


@dataclass
class AgentRunResult:
    """Aggregate outcome of one agent run."""

    effects: list[AgentEffect]
    cost_usd: float
    turns: int
    stopped_reason: str

    @property
    def has_terminal(self) -> bool:
        return any(e.kind in ("final_answer", "escalate", "abandon") for e in self.effects)

    @property
    def has_async_dispatch(self) -> bool:
        return any(e.kind == "ask_dispatched" for e in self.effects)


@dataclass
class AgentRunInput:
    task: ClarificationTask
    history: Sequence[TaskStep]
    issue_summary: str
    repo_workspace: str | None


class ClarificationAgent:
    """Drives one ``ClarificationTask`` via continuous reasoning."""

    agent_key = "clarification-agent"

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
        self._max_turns = max_turns or _agent_max_turns(config) or 30

    async def run(self, inp: AgentRunInput) -> AgentRunResult:
        """Run one agent session. Returns the side-effects observed."""
        prompt = self._render_prompt(inp)
        effects: list[AgentEffect] = []
        result = await self._call_model(prompt, inp, effects)
        return AgentRunResult(
            effects=effects,
            cost_usd=result.cost_usd,
            turns=result.turns,
            stopped_reason=result.stopped_reason,
        )

    # ---------------------------------------------------------------- internals

    async def _call_model(
        self,
        prompt: str,
        inp: AgentRunInput,
        effects: list[AgentEffect],
    ) -> Any:
        mcp_servers, allowed = self._build_mcp(inp, effects)

        request = CodeAgentRequest(
            agent_key=self.agent_key,
            system_prompt=self._prompts.render(
                _PROMPT_NAME,
                fallback=_FALLBACK_PROMPT,
                untrusted_warning=SYSTEM_PROMPT_ABOUT_UNTRUSTED,
            ),
            user_prompt=prompt,
            working_dir=inp.repo_workspace,
            max_turns=self._max_turns,
            model=self._resolve_model(),
        )
        request.extras["mcp_servers"] = mcp_servers
        request.extras["allowed_tool_names"] = allowed
        return await self._code_agent.run_task(request)

    def _build_mcp(
        self,
        inp: AgentRunInput,
        effects: list[AgentEffect],
    ) -> tuple[dict[str, McpSdkServerConfig], list[str]]:
        """Build the MCP servers exposing the clarification tools.

        Each tool is wrapped here so its handler can:
        * mutate ``effects`` (for the orchestrator to act on)
        * use the live communicator/config
        * see the in-progress task

        Why not use the existing ToolRegistry? Because that registry's
        SYNC/ASYNC/META abstraction is geared toward an
        orchestrator-driven loop. Here the LLM drives, so the tool
        descriptions / shapes are tuned to that flow.
        """
        servers: dict[str, McpSdkServerConfig] = {}
        allowed: list[str] = []

        # ---- Researcher MCP (read-only) ----
        if self._researcher is not None:
            servers["virtual_dev_researcher"] = self._researcher.build_mcp_server()
            allowed.extend([
                "mcp__virtual_dev_researcher__search_code",
                "mcp__virtual_dev_researcher__read_file",
                "mcp__virtual_dev_researcher__kb_search",
                "mcp__virtual_dev_researcher__kb_fetch_page_by_url",
                "mcp__virtual_dev_researcher__search_mr_history",
            ])

        # ---- Clarification tools (SYNC + ASYNC + META) ----
        clar_server = self._build_clarification_server(inp, effects)
        servers["virtual_dev_clarification"] = clar_server
        allowed.extend([
            "mcp__virtual_dev_clarification__find_mm_user_by_name",
            "mcp__virtual_dev_clarification__lookup_mm_user",
            "mcp__virtual_dev_clarification__ask_mm_user",
            "mcp__virtual_dev_clarification__submit_final_answer",
            "mcp__virtual_dev_clarification__escalate_to_lead",
            "mcp__virtual_dev_clarification__abandon",
        ])

        # ---- Filesystem tools ----
        allowed.extend(["Read", "Glob", "Grep"])

        return servers, allowed

    def _build_clarification_server(
        self, inp: AgentRunInput, effects: list[AgentEffect],
    ) -> McpSdkServerConfig:
        communicator = self._communicator

        # ---- find_mm_user_by_name (SYNC) ----
        @tool(
            "find_mm_user_by_name",
            "Fuzzy-search Mattermost directory by name (Russian or "
            "English). Matches first_name / last_name / nickname / "
            "username. Use the surname when possible — short Russian "
            "first names are too ambiguous. Returns 0..N candidates "
            "with handle, full name, position. **Use BEFORE asking "
            "anyone about a person whose handle you don't know.**",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": ["integer", "null"]},
                },
                "required": ["query"],
            },
        )
        async def _find(args: dict[str, Any]) -> dict[str, Any]:
            query = str(args.get("query") or "").strip()
            if not query:
                return _wrap({"matches": [], "reason": "empty_query"})
            raw_limit = args.get("limit")
            try:
                limit = int(raw_limit) if raw_limit is not None else 10
            except (TypeError, ValueError):
                limit = 10
            limit = max(1, min(limit, 25))
            users = await communicator.search_users_by_name(query, limit=limit)
            return _wrap({
                "query": query,
                "matches": [
                    {
                        "handle": u.username, "mm_user_id": u.id,
                        "email": u.email,
                        "first_name": u.first_name,
                        "last_name": u.last_name,
                        "display_name": u.display_name,
                        "position": u.position,
                    }
                    for u in users
                ],
            })

        # ---- lookup_mm_user (SYNC) ----
        @tool(
            "lookup_mm_user",
            "Resolve a Mattermost user by exact handle or email. "
            "Returns {found: bool, mm_user_id?, display_name?}. Use "
            "AFTER you've narrowed a candidate via "
            "find_mm_user_by_name, or when a human DM'd you a "
            "@-handle.",
            {
                "type": "object",
                "properties": {
                    "handle": {"type": ["string", "null"]},
                    "email": {"type": ["string", "null"]},
                },
            },
        )
        async def _lookup(args: dict[str, Any]) -> dict[str, Any]:
            handle = (args.get("handle") or "").strip().lstrip("@") or None
            email = (args.get("email") or "").strip() or None
            if not handle and not email:
                return _wrap({"found": False, "reason": "no_handle_or_email"})
            uid = await communicator.resolve_user_id(username=handle, email=email)
            if uid is None:
                return _wrap({"found": False, "reason": "not_found"})
            return _wrap({
                "found": True, "mm_user_id": uid,
                "display_name": handle or email,
            })

        # ---- ask_mm_user (ASYNC) ----
        @tool(
            "ask_mm_user",
            "DM a specific Mattermost user one question. Pass "
            "to_handle OR to_email (one of them). The message is "
            "sent verbatim — write it the way the bot should sound "
            "in Mattermost (Russian for 2GIS tickets), polite, "
            "concise, with the ticket number and what you need. "
            "**THIS IS ASYNC**: after you call this tool, end your "
            "turn — you'll be re-invoked when the human's reply "
            "comes in. Do not call any other tools after this one "
            "in the same turn.",
            {
                "type": "object",
                "properties": {
                    "to_handle": {"type": ["string", "null"]},
                    "to_email": {"type": ["string", "null"]},
                    "message": {"type": "string"},
                    "dedupe_key": {"type": ["string", "null"]},
                },
                "required": ["message"],
            },
        )
        async def _ask(args: dict[str, Any]) -> dict[str, Any]:
            handle = (args.get("to_handle") or "").strip().lstrip("@") or None
            email = (args.get("to_email") or "").strip() or None
            message = str(args.get("message") or "").strip()
            dedupe_key = (args.get("dedupe_key") or "").strip() or None
            if not message:
                return _wrap({"sent": False, "reason": "empty_message"})
            if not handle and not email:
                return _wrap({"sent": False, "reason": "missing_target"})
            uid = await communicator.resolve_user_id(username=handle, email=email)
            if uid is None:
                label = handle or email or ""
                return _wrap({
                    "sent": False, "reason": f"unresolved:{label}",
                    "hint": (
                        "Don't guess transliterations — call "
                        "find_mm_user_by_name first, or ask the "
                        "issue reporter for a confirmed handle."
                    ),
                })
            outcome = await communicator.send_dm(uid, message)
            if not outcome.sent or outcome.message is None:
                return _wrap({
                    "sent": False,
                    "reason": f"send_failed:{outcome.skip_reason or 'unknown'}",
                })
            effects.append(AgentEffect(
                kind="ask_dispatched",
                payload={
                    "asked_post_id": outcome.message.id,
                    "channel_id": outcome.message.channel_id,
                    "target_user_id": uid,
                    "target_username": handle,
                    "target_email": email,
                    "asked_text": message,
                    "dedupe_key": dedupe_key,
                },
            ))
            return _wrap({
                "sent": True,
                "to_user_id": uid,
                "channel_id": outcome.message.channel_id,
                "asked_post_id": outcome.message.id,
                "instruction": (
                    "DM dispatched. END YOUR TURN now. The "
                    "orchestrator will re-invoke you with the "
                    "human's reply when it arrives."
                ),
            })

        # ---- submit_final_answer (META: closes the task) ----
        @tool(
            "submit_final_answer",
            "Mark this task solved and store the final answer. Call "
            "this when you're confident the answer is in hand. The "
            "answer goes into the task's record and (if this is a "
            "top-level task) gets folded into the issue description "
            "so the analyst can re-plan.",
            {
                "type": "object",
                "properties": {
                    "final_answer": {
                        "type": "string",
                        "description": (
                            "Self-contained synthesis. The analyst "
                            "reads this cold without the chat "
                            "history, so embed concrete details "
                            "(handle, body, endpoint, etc.) and "
                            "write in the issue's language."
                        ),
                    },
                    "confidence": {
                        "type": "number",
                        "description": (
                            "0..1. Below 0.6 the orchestrator may "
                            "flag for sanity review."
                        ),
                    },
                    "reasoning": {"type": ["string", "null"]},
                },
                "required": ["final_answer", "confidence"],
            },
        )
        async def _submit(args: dict[str, Any]) -> dict[str, Any]:
            answer = str(args.get("final_answer") or "").strip()
            try:
                confidence = float(args.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            confidence = max(0.0, min(1.0, confidence))
            if not answer:
                return _wrap({"recorded": False, "reason": "empty_answer"})
            effects.append(AgentEffect(
                kind="final_answer",
                payload={
                    "final_answer": answer,
                    "confidence": confidence,
                    "reasoning": str(args.get("reasoning") or ""),
                },
            ))
            return _wrap({
                "recorded": True,
                "instruction": "Task is solved. End your turn.",
            })

        # ---- escalate_to_lead (META) ----
        @tool(
            "escalate_to_lead",
            "Give up and DM the team-lead with the chain. Use when "
            "you're truly stuck: respondent doesn't know, no leads "
            "left to ask, you need a human's intent decision and the "
            "issue author is unreachable.",
            {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        )
        async def _escalate(args: dict[str, Any]) -> dict[str, Any]:
            reason = str(args.get("reason") or "").strip() or "no_reason"
            effects.append(AgentEffect(
                kind="escalate", payload={"reason": reason},
            ))
            return _wrap({
                "recorded": True,
                "instruction": "Escalation queued. End your turn.",
            })

        # ---- abandon (META) ----
        @tool(
            "abandon",
            "Soft give-up — close without escalating. Use when the "
            "task turned out to be unnecessary (issue self-"
            "contradicts, became obsolete, you've concluded human "
            "follow-up is genuinely not needed).",
            {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        )
        async def _abandon(args: dict[str, Any]) -> dict[str, Any]:
            reason = str(args.get("reason") or "").strip() or "no_reason"
            effects.append(AgentEffect(
                kind="abandon", payload={"reason": reason},
            ))
            return _wrap({
                "recorded": True,
                "instruction": "Task abandoned. End your turn.",
            })

        return create_sdk_mcp_server(
            name="virtual_dev_clarification", version="0.1.0",
            tools=[_find, _lookup, _ask, _submit, _escalate, _abandon],
        )

    def _resolve_model(self) -> str:
        agent_cfg = self._config.agents.agents.get(
            self.agent_key.replace("-", "_"),
        )
        if agent_cfg is None:
            return self._config.agents.models.default
        chosen = agent_cfg.model or "default"
        return getattr(
            self._config.agents.models, chosen,
            self._config.agents.models.default,
        )

    def _render_prompt(self, inp: AgentRunInput) -> str:
        parts: list[str] = []
        parts.append("# Resolve this clarification task")
        parts.append("")
        parts.append("## What we need to learn")
        parts.append(inp.task.question.strip())
        if inp.task.info_source:
            parts.append("")
            parts.append(
                f"**Hint about who/what should answer:** "
                f"{inp.task.info_source} "
                f"(class: {inp.task.info_source_class or '?'})"
            )
        parts.append("")

        if inp.issue_summary.strip():
            parts.append("## Original issue (for context)")
            wrapped = self._filter.wrap(inp.issue_summary, source="issue:summary")
            parts.append(wrapped.wrapped_text)
            parts.append("")

        # The agent's own prior reasoning + the human replies it has
        # received. Re-rendered every invocation so the LLM has full
        # continuity even though the SDK session itself is one-shot.
        parts.append("## Everything you've done on this task so far")
        if not inp.history:
            parts.append("_(this is your first invocation — nothing yet)_")
        else:
            for step in inp.history:
                parts.append(self._render_step(step))
        parts.append("")

        parts.append("## How to proceed")
        parts.append(
            "Decide the next move. You may chain SYNC tools "
            "(find_mm_user_by_name, lookup_mm_user, Read/Glob/Grep, "
            "Researcher MCP) freely within this one turn — read "
            "results, think, call another, etc. When you reach a "
            "decision point, call exactly one of:"
        )
        parts.append(
            "- `ask_mm_user` (ASYNC) — DM a person; END YOUR TURN "
            "after; you'll be re-invoked when they reply."
        )
        parts.append(
            "- `submit_final_answer` — task solved; pass the "
            "synthesized answer + confidence."
        )
        parts.append(
            "- `escalate_to_lead` — truly stuck; team-lead will be "
            "DM'd with the chain."
        )
        parts.append(
            "- `abandon` — task is no longer relevant (issue "
            "self-contradicts, etc.)."
        )
        parts.append("")
        parts.append(
            "**Iteration #{} on this task.** Don't loop forever — "
            "if you've tried multiple angles and nothing's working, "
            "escalate.".format(inp.task.iteration_count)
        )
        return "\n".join(parts)

    def _render_step(self, step: TaskStep) -> str:
        ts = step.timestamp.strftime("%H:%M:%S") if step.timestamp else ""
        head = f"**[{step.seq}] {step.kind.value}** ({ts})"
        body = step.text.strip()
        if not body:
            return head
        if step.kind in (TaskStepKind.HUMAN_REPLIED, TaskStepKind.STALE_FRAGMENT):
            wrapped = self._filter.wrap(body, source=f"task:step:{step.seq}")
            return head + "\n" + wrapped.wrapped_text
        # Tool-result and bot_asked steps may include JSON blobs;
        # display them raw so the LLM sees what it saw last time.
        if len(body) > 2000:
            body = body[:2000] + "\n[truncated]"
        return head + "\n" + body


def _wrap(payload: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{
        "type": "text",
        "text": json.dumps(payload, ensure_ascii=False),
    }]}


def _agent_max_turns(config: AppConfig) -> int | None:
    cfg = config.agents.agents.get("clarification_agent")
    return cfg.max_iterations_per_task if cfg is not None else None


__all__ = [
    "AgentEffect",
    "AgentRunInput",
    "AgentRunResult",
    "ClarificationAgent",
]
