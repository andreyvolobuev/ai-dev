"""ClarificationToolPicker — decides ONE next tool per task per turn.

Replaces the old ClarificationPlanner which had 6 baked-in actions.
Now the actions ARE the tools — every choice goes through the
registry, so adding a new capability is purely additive.

The picker's interface is tiny: it sees the live task, its ancestor
chain, the issue context, the list of tools (with their descriptions
and schemas), and the list of tools already tried on this task. It
returns ``ToolInvocation(tool, params, reasoning)`` — orchestrator
runs the tool.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from virtual_dev.application.services.clarification.tools import Tool

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig  # type: ignore[attr-defined]
from loguru import logger

from virtual_dev.application.services.agent_trace import AgentTrace
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
    ToolInvocation,
)
from virtual_dev.domain.ports.code_agent import CodeAgentPort, CodeAgentRequest
from virtual_dev.infrastructure.config import AppConfig


_PROMPT_NAME = "clarification_tool_picker"
_FALLBACK_PROMPT = (
    "You are the Clarification Tool Picker. Pick ONE next tool to run "
    "for this task. Call submit_pick exactly once.\n\n{untrusted_warning}"
)


_SUBMIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool": {
            "type": "string",
            "description": (
                "Name of one of the tools listed in the user prompt's "
                "'Available tools' section. Exact match required."
            ),
        },
        "params": {
            "type": "object",
            "description": (
                "JSON object matching the chosen tool's schema. The "
                "orchestrator validates this against the tool's schema "
                "before running."
            ),
        },
        "reasoning": {
            "type": "string",
            "description": (
                "1-3 sentences explaining why this tool, why now, and "
                "what we expect to learn. Audit trail for humans."
            ),
        },
    },
    "required": ["tool", "reasoning"],
}


@dataclass
class PickerInput:
    task: ClarificationTask
    chain: Sequence[ClarificationTask]   # root → … → parent → task
    history: Sequence[TaskStep]
    issue_summary: str
    repo_workspace: str | None
    available_tools: "Sequence[Tool]"


class ClarificationToolPicker:
    """LLM-backed tool selector — one ``ToolInvocation`` per call."""

    agent_key = "clarification-tool-picker"

    def __init__(
        self,
        *,
        code_agent: CodeAgentPort,
        config: AppConfig,
        prompts_loader: PromptsLoader,
        researcher: ResearcherToolkit | None,
        injection_filter: InjectionFilter | None = None,
        trace: AgentTrace | None = None,
        max_turns: int | None = None,
    ) -> None:
        self._code_agent = code_agent
        self._config = config
        self._prompts = prompts_loader
        self._researcher = researcher
        self._filter = injection_filter or InjectionFilter()
        self._trace = trace
        self._max_turns = max_turns or _picker_max_turns(config) or 12

    async def pick(self, inp: PickerInput) -> ToolInvocation:
        prompt = self._render_prompt(inp)
        captured, result = await self._call_model(prompt, inp.repo_workspace)

        if not captured:
            logger.warning(
                "ToolPicker: model finished without calling submit_pick "
                "(stop={})", result.stopped_reason,
            )
            return ToolInvocation(
                tool="escalate_to_lead",
                params={"reason": "tool-picker did not produce a pick"},
                reasoning="model-did-not-submit",
            )

        tool_name = str(captured.get("tool") or "").strip()
        if not tool_name:
            return ToolInvocation(
                tool="escalate_to_lead",
                params={"reason": "tool-picker returned empty tool name"},
                reasoning="empty tool name",
            )
        params = captured.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        return ToolInvocation(
            tool=tool_name,
            params=params,
            reasoning=str(captured.get("reasoning") or ""),
        )

    # ---------------------------------------------------------------- internals

    async def _call_model(
        self, prompt: str, workspace: str | None,
    ) -> tuple[dict[str, Any], Any]:
        captured: dict[str, Any] = {}

        @tool(
            "submit_pick",
            "Submit your single tool pick for this task. Call exactly once.",
            _SUBMIT_SCHEMA,
        )
        async def _submit(args: dict[str, Any]) -> dict[str, Any]:
            captured.clear()
            captured.update(args)
            return {"content": [{"type": "text", "text": "Pick recorded."}]}

        submit_server = create_sdk_mcp_server(
            name="virtual_dev_picker_submit", version="0.1.0",
            tools=[_submit],
        )
        mcp_servers: dict[str, McpSdkServerConfig] = {
            "virtual_dev_picker_submit": submit_server,
        }
        allowed = ["mcp__virtual_dev_picker_submit__submit_pick"]

        # Researcher MCP (read-only research tools).
        if self._researcher is not None:
            mcp_servers["virtual_dev_researcher"] = self._researcher.build_mcp_server()
            allowed.extend([
                "mcp__virtual_dev_researcher__search_code",
                "mcp__virtual_dev_researcher__read_file",
                "mcp__virtual_dev_researcher__kb_search",
                "mcp__virtual_dev_researcher__kb_fetch_page_by_url",
                "mcp__virtual_dev_researcher__search_mr_history",
            ])

        # Filesystem.
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
        agent_cfg = self._config.agents.agents.get(
            self.agent_key.replace("-", "_"),
        )
        if agent_cfg is None:
            return self._config.agents.models.default
        chosen = agent_cfg.model or "default"
        return getattr(
            self._config.agents.models, chosen, self._config.agents.models.default,
        )

    def _render_prompt(self, inp: PickerInput) -> str:
        parts: list[str] = []
        parts.append("# Pick the next tool to run for this task")
        parts.append("")

        # Chain of ancestors.
        if len(inp.chain) > 1:
            parts.append("## Ancestor chain (root → parent)")
            for i, t in enumerate(inp.chain[:-1]):
                parts.append(
                    f"- depth {t.depth}, task #{t.id}: «{t.question.strip()}»"
                    f"{' [solved]' if t.is_solved else ''}"
                )
            parts.append("")

        parts.append("## This task")
        parts.append(f"**Question:** {inp.task.question.strip()}")
        if inp.task.info_source:
            parts.append(
                f"**Known info_source:** {inp.task.info_source} "
                f"(class: {inp.task.info_source_class or '?'})"
            )
        if inp.task.current_response:
            parts.append("**Latest response (untrusted):**")
            wrapped = self._filter.wrap(
                inp.task.current_response, source="task:current_response",
            )
            parts.append(wrapped.wrapped_text)
        if inp.task.tools_tried:
            parts.append(
                f"**Tools already tried (without solving):** "
                f"{', '.join(inp.task.tools_tried)}"
            )
        parts.append(f"**Iteration #{inp.task.iteration_count}**")
        parts.append("")

        if inp.issue_summary.strip():
            parts.append("## Original issue")
            wrapped = self._filter.wrap(inp.issue_summary, source="issue:summary")
            parts.append(wrapped.wrapped_text)
            parts.append("")

        # History — append-only timeline of what's been tried.
        parts.append("## History so far (oldest first)")
        if not inp.history:
            parts.append("_(no steps yet — this is the first decision for this task)_")
        else:
            for step in inp.history:
                parts.append(self._render_step(step))
        parts.append("")

        # Available tools — the planner must pick one of these by name.
        parts.append("## Available tools")
        for t in inp.available_tools:
            parts.append(
                f"### `{t.name}` (mode={t.mode.value})\n{t.description}\n"
                f"_schema_: ```json\n{_compact(t.schema)}\n```"
            )
        parts.append("")

        parts.append(
            "Decide: which one tool gives us the most progress on this "
            "task? Pick exactly one and call `submit_pick(tool, params, "
            "reasoning)`. If you've genuinely run out of options, pick "
            "`escalate_to_lead` (or `abandon` if no human follow-up is "
            "needed). Don't repeat a tool already in 'tools tried' "
            "without new evidence."
        )
        return "\n".join(parts)

    def _render_step(self, step: TaskStep) -> str:
        head = f"**[{step.seq}] {step.kind.value}** ({step.timestamp.strftime('%H:%M:%S') if step.timestamp else ''})"
        body = step.text.strip()
        if not body:
            return head
        if step.kind in (TaskStepKind.HUMAN_REPLIED, TaskStepKind.STALE_FRAGMENT):
            wrapped = self._filter.wrap(body, source=f"task:step:{step.seq}")
            return head + "\n" + wrapped.wrapped_text
        return head + "\n" + body


def _compact(schema: dict[str, Any]) -> str:
    import json
    return json.dumps(schema, ensure_ascii=False, indent=None, separators=(",", ":"))


def _picker_max_turns(config: AppConfig) -> int | None:
    cfg = config.agents.agents.get("clarification_tool_picker")
    return cfg.max_iterations_per_task if cfg is not None else None


__all__ = ["ClarificationToolPicker", "PickerInput"]
