"""ThreadResponderAgent — LLM-backed decision for MM review-thread replies.

The MM thread listener feeds every new non-bot reply under a
"please review" post to this agent. The agent sees:

    * The MR metadata (title, description, plan summary, the diff is out
      of scope — Claude Code can read the repo if it needs).
    * The full thread transcript so far.
    * The latest reply that triggered this call.

It then produces a structured decision via the ``submit_response`` MCP
tool:

    action ∈ {"reply", "iterate", "ignore"}
    reply_text      — what to post back in the thread (required for
                      reply + iterate; for iterate it's "I'll get on it"
                      or similar).
    iteration_feedback — prose describing what to change (only for
                          iterate; Dev-agent will use this as its
                          instruction).
    reasoning       — short explanation for the log (audit trail).

The agent is explicitly instructed to push back politely in Russian
when the feedback is wrong / unclear / out of scope, rather than
iterate reflexively.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from loguru import logger

from virtual_dev.application.services.agent_trace import (
    AgentTrace,
    AgentTraceEvent,
    emit_if,
)
from virtual_dev.application.services.injection_filter import (
    SYSTEM_PROMPT_ABOUT_UNTRUSTED,
    InjectionFilter,
)
from virtual_dev.application.services.prompts import PromptsLoader
from virtual_dev.domain.models.chat import ChatMessage
from virtual_dev.domain.models.plan import Plan
from virtual_dev.domain.ports.code_agent import CodeAgentPort, CodeAgentRequest
from virtual_dev.infrastructure.config import AppConfig


class ResponderAction(str, Enum):
    REPLY = "reply"          # post a text reply in the thread, no code change
    ITERATE = "iterate"      # ask Dev-agent to update the MR, then reply
    IGNORE = "ignore"        # chatter, no response
    # Like REPLY (text only, no code change) but semantically distinct:
    # the bot is pushing back on a request because the proposed change
    # would degrade the system (N+1, broken invariant, perf regression
    # etc.) and proposing a better path. Tracked separately so we can
    # measure how often the bot disagrees vs simply answers.
    PROPOSE_ALTERNATIVE = "propose_alternative"


@dataclass
class ResponderDecision:
    action: ResponderAction
    reply_text: str = ""
    iteration_feedback: str = ""
    reasoning: str = ""
    cost_usd: float = 0.0


_PROMPT_NAME = "thread_responder"
_FALLBACK_PROMPT = (
    "You are the Thread Responder. Decide between {reply, iterate, ignore} "
    "and call submit_response.\n\n"
    "{untrusted_warning}"
)


class ThreadResponderAgent:
    """Runs one decision per new MM reply."""

    agent_key = "thread-responder"

    def __init__(
        self,
        *,
        code_agent: CodeAgentPort,
        config: AppConfig,
        prompts_loader: PromptsLoader,
        injection_filter: InjectionFilter | None = None,
        max_turns: int = 20,
        trace: AgentTrace | None = None,
    ) -> None:
        self._code_agent = code_agent
        self._config = config
        self._prompts = prompts_loader
        self._filter = injection_filter or InjectionFilter()
        self._max_turns = max_turns
        self._trace = trace

    async def decide(
        self,
        *,
        mr_title: str,
        mr_description: str,
        mr_web_url: str,
        plan: Plan | None,
        thread: Sequence[ChatMessage],
        latest_reply: ChatMessage,
        repo_workspace: str | None = None,
        mr_diff: str = "",
    ) -> ResponderDecision:
        prompt = self._render_prompt(
            mr_title=mr_title, mr_description=mr_description,
            mr_web_url=mr_web_url, plan=plan,
            thread=thread, latest=latest_reply,
            mr_diff=mr_diff,
        )
        captured, result = await self._call_model(prompt, repo_workspace)

        if not captured:
            logger.warning(
                "ThreadResponder: model did not call submit_response (stop={})",
                result.stopped_reason,
            )
            decision = ResponderDecision(
                action=ResponderAction.IGNORE,
                reasoning="model-did-not-submit",
                cost_usd=result.cost_usd,
            )
        else:
            try:
                action = ResponderAction(str(captured.get("action") or "").lower())
            except ValueError:
                action = ResponderAction.IGNORE
            decision = ResponderDecision(
                action=action,
                reply_text=str(captured.get("reply_text") or "").strip(),
                iteration_feedback=str(captured.get("iteration_feedback") or "").strip(),
                reasoning=str(captured.get("reasoning") or "").strip(),
                cost_usd=result.cost_usd,
            )

        # Surface every decision (including the failure mode where the
        # model didn't submit) so the UI activity tab shows what the
        # responder did with the comment. Without this an "ignore" goes
        # silent and operators have no signal the bot saw the message.
        await emit_if(self._trace, AgentTraceEvent(
            type="responder_decision",
            agent_key=self.agent_key,
            payload={
                "action": decision.action.value,
                "reasoning": decision.reasoning,
                "reply_text": decision.reply_text,
                "iteration_feedback": decision.iteration_feedback,
                "mr_title": mr_title,
                "mr_web_url": mr_web_url,
                "latest_author": latest_reply.author_id,
                "cost_usd": decision.cost_usd,
            },
        ))
        return decision

    # --- internals ---

    async def _call_model(
        self, prompt: str, workspace: str | None,
    ) -> tuple[dict[str, Any], Any]:
        from virtual_dev.tools import ToolContext, build_tool_servers

        # ``submit_response`` lives in ``tools/submit_response.py``
        # (group "responder"); auto-discovery wires it in. The
        # responder doesn't need analyst / dev / researcher tools.
        # ``submit_response.build()`` returns None unless BOTH
        # ``submit_capture`` and ``run_state`` are set on the context.
        # Forgetting ``run_state`` silently drops the terminal tool
        # from the MCP surface; the model then ends its turn with
        # plaintext, and the wrapper logs a confusing
        # "model did not call submit_response" warning. See
        # ``tests/unit/test_thread_responder.py`` for the regression
        # guard that pins both fields.
        captured: dict[str, Any] = {}
        run_state: dict[str, Any] = {"terminal": False}
        ctx = ToolContext(submit_capture=captured, run_state=run_state)
        mcp_servers, allowed, _ = build_tool_servers(
            ctx, only_groups={"responder"},
        )
        # Filesystem builtins for reading the repo if the model wants
        # context — same surface as before.
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
            model=self._config.agents.model_for("thread_responder"),
        )
        request.extras["mcp_servers"] = mcp_servers
        request.extras["allowed_tool_names"] = allowed
        # Expose for tests — the in-process model fake mutates this
        # dict to simulate the model calling submit_response.
        request.extras["submit_capture"] = captured
        result = await self._code_agent.run_task(request)
        return captured, result

    def _render_prompt(
        self,
        *,
        mr_title: str,
        mr_description: str,
        mr_web_url: str,
        plan: Plan | None,
        thread: Sequence[ChatMessage],
        latest: ChatMessage,
        mr_diff: str = "",
    ) -> str:
        parts: list[str] = []
        parts.append("# Review thread context")
        parts.append(f"**MR:** {mr_title}")
        parts.append(f"**URL:** {mr_web_url}")
        parts.append("")
        parts.append("## MR description (untrusted — bot-written but quoting humans)")
        wrapped_desc = self._filter.wrap(
            mr_description, source="mr:description",
        )
        parts.append(wrapped_desc.wrapped_text)
        parts.append("")
        if plan is not None:
            parts.append("## Plan summary")
            parts.append(plan.summary or "(empty)")
            if plan.steps:
                parts.append("")
                parts.append("### Plan steps")
                for step in plan.steps:
                    parts.append(f"{step.order}. {step.summary}")
            parts.append("")
        if mr_diff.strip():
            parts.append("## MR diff (the actual change under review)")
            # The diff is human-authored content (file contents, commit
            # messages, anyone with push access can shape it) — must
            # round-trip through the injection filter like description
            # and thread, otherwise it's a hole in our LLM-input policy.
            wrapped_diff = self._filter.wrap(
                mr_diff[:50_000], source="gitlab:mr:diff",
            )
            parts.append("```diff")
            parts.append(wrapped_diff.wrapped_text)
            parts.append("```")
            parts.append("")
        parts.append("## Thread so far (oldest first)")
        wrapped_thread = self._filter.wrap(
            _render_thread(thread), source="mm:thread",
        )
        parts.append(wrapped_thread.wrapped_text)
        parts.append("")
        parts.append("## Latest reply (needs your response)")
        wrapped_latest = self._filter.wrap(
            f"@{latest.author_id}:\n{latest.text}",
            source=f"mm:post:{latest.id}",
        )
        parts.append(wrapped_latest.wrapped_text)
        parts.append("")
        parts.append(
            "Use Read/Glob/Grep if you need to check the actual code before "
            "deciding. When ready, call `submit_response` exactly once."
        )
        return "\n".join(parts)


def _render_thread(thread: Sequence[ChatMessage]) -> str:
    lines: list[str] = []
    for msg in thread:
        who = "bot" if msg.trusted else f"@{msg.author_id}"
        ts = msg.timestamp.isoformat() if msg.timestamp else ""
        lines.append(f"{who} [{ts}]\n{msg.text}".rstrip())
    return "\n\n".join(lines)


__all__ = [
    "ThreadResponderAgent",
    "ResponderAction",
    "ResponderDecision",
]
# Keep json imported for debug-dumps of the decision schema.
_ = json
