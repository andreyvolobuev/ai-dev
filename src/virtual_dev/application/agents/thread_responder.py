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
from typing import TYPE_CHECKING, Any

from loguru import logger

from virtual_dev.application.services.agent_trace import (
    AgentTrace,
    AgentTraceEvent,
    emit_if,
)
from virtual_dev.application.services.ci_state import CiReading
from virtual_dev.application.services.injection_filter import (
    SYSTEM_PROMPT_ABOUT_UNTRUSTED,
    InjectionFilter,
)
from virtual_dev.application.services.prompts import PromptsLoader
from virtual_dev.application.services.repo_instructions import render_repo_instructions
from virtual_dev.application.services.transcript import local_time, render_transcript
from virtual_dev.domain.models.chat import ChatMessage
from virtual_dev.domain.models.plan import Plan
from virtual_dev.domain.ports.code_agent import CodeAgentPort, CodeAgentRequest
from virtual_dev.domain.ports.professor_wiki import MANDATORY_REVIEW_TOPIC, ProfessorWikiPort
from virtual_dev.domain.ports.vcs import VcsPort
from virtual_dev.infrastructure.config import AppConfig, Settings
if TYPE_CHECKING:
    from virtual_dev.application.services import ResearcherToolkit
from virtual_dev.tools import merge_professor_mcp


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
    # A file to post with reply_text: (name, text).
    attachment: tuple[str, str] | None = None


_PROMPT_NAME = "thread_responder"
_SHARED_TOOLS = frozenset({"read_merge_request", "fetch_url"})
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
        researcher: ResearcherToolkit | None = None,
        # Optional for compatibility with existing constructions; the
        # container always passes it. Wiki tools are only added when it
        # is present and Professor is configured.
        settings: Settings | None = None,
        # Mandatory-review provider (Stage 2). Optional for the same
        # reason; when absent, no article is injected and no preflight
        # runs.
        wiki: ProfessorWikiPort | None = None,
    ) -> None:
        self._code_agent = code_agent
        self._researcher = researcher
        self._settings = settings
        self._config = config
        self._prompts = prompts_loader
        self._filter = injection_filter or InjectionFilter()
        self._max_turns = max_turns
        self._trace = trace
        self._settings = settings
        self._wiki = wiki

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
        mr_source_branch: str = "",
        mr_target_branch: str = "",
        ci: CiReading | None = None,
        work_state: str = "",
        linked_notes: Sequence[tuple[str, str]] = (),
    ) -> ResponderDecision:
        # Fail-closed preflight (Stage 5): обязательный стандарт ревью
        # должен быть в контексте ДО принятия решения. При недоступности
        # Wiki исключение уходит наружу — вызывающий (reviewer polling)
        # не делает claim/ack, lease истекает, шина делает redelivery.
        # Пустой polling сюда не доходит: decide() зовётся только на
        # реальный ответ в треде.
        wiki_block = await self._mandatory_wiki_block()
        prompt = self._render_prompt(
            mr_title=mr_title, mr_description=mr_description,
            mr_web_url=mr_web_url, plan=plan,
            thread=thread, latest=latest_reply,
            mr_diff=mr_diff,
            mr_source_branch=mr_source_branch,
            mr_target_branch=mr_target_branch,
            ci=ci,
            work_state=work_state,
            linked_notes=linked_notes,
        )
        captured, result = await self._call_model(prompt, repo_workspace, wiki_block=wiki_block)

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
            attachment_name = str(captured.get("attachment_name") or "").strip()
            attachment_content = str(captured.get("attachment_content") or "")
            decision = ResponderDecision(
                action=action,
                reply_text=str(captured.get("reply_text") or "").strip(),
                iteration_feedback=str(captured.get("iteration_feedback") or "").strip(),
                reasoning=str(captured.get("reasoning") or "").strip(),
                cost_usd=result.cost_usd,
                attachment=(
                    (attachment_name, attachment_content)
                    if attachment_name and attachment_content.strip() else None
                ),
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

    def _timezone(self) -> str:
        return self._config.agents.working_hours.timezone

    async def _call_model(
        self, prompt: str, workspace: str | None, wiki_block: str = "",
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
        ctx = ToolContext(
            submit_capture=captured, run_state=run_state,
            researcher=self._researcher, settings=self._settings,
        )
        mcp_servers, allowed, _ = build_tool_servers(
            ctx, only_groups={"responder", "shared"},
        )
        # Links in the thread must be followed, not handed back to the
        # human («напиши суть в треде»): MR links and comments through the
        # API, everything else as a plain fetch. The rest of the shared
        # group (code search, KB, attachments) is the analyst's surface.
        allowed = [
            name for name in allowed
            if not name.startswith("mcp__virtual_dev_shared__")
            or name.removeprefix("mcp__virtual_dev_shared__") in _SHARED_TOOLS
        ]
        # Optional remote LLM-Wiki surface for follow-up wiki lookups
        # while making the review decision. Disabled unless Professor
        # URL + token are both configured.
        if self._settings is not None:
            mcp_servers, allowed = merge_professor_mcp(
                self._settings, mcp_servers, allowed,
            )
        # Filesystem builtins for reading the repo if the model wants
        # context — same surface as before.
        allowed.extend(["Read", "Glob", "Grep"])

        # Base prompt + mandatory wiki standard (Stage 5); the repo's own
        # rules (rainbow keeps «пересобери ветку» there) go after it — the
        # responder turns asks into tasks and must read them the way the
        # team does.
        system_prompt = self._compose_system_prompt(wiki_block)
        repo_rules = render_repo_instructions(workspace)
        if repo_rules:
            system_prompt = f"{system_prompt}\n\n{repo_rules}"
        request = CodeAgentRequest(
            agent_key=self.agent_key,
            system_prompt=system_prompt,
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

    def _compose_system_prompt(self, wiki_block: str) -> str:
        """Базовый промпт + обязательный стандарт ревью последним блоком.

        Блок идёт после `{untrusted_warning}` — как и у Dev, «стандарт
        читают последним, применяют всегда».
        """
        system_prompt = self._prompts.render(
            _PROMPT_NAME,
            fallback=_FALLBACK_PROMPT,
            untrusted_warning=SYSTEM_PROMPT_ABOUT_UNTRUSTED,
        )
        if wiki_block:
            system_prompt = f"{system_prompt}\n\n{wiki_block}"
        return system_prompt

    async def _mandatory_wiki_block(self) -> str:
        """Полный текст обязательного стандарта ревью из LLM-Wiki.

        ``None`` → интеграция выключена (пустой блок). Ошибка доставки —
        ``WikiUnavailableError`` наружу: решение не принимается, вызывающий
        код не делает claim/ack (fail-closed, Stage 5).
        """
        if self._wiki is None:
            return ""
        article = await self._wiki.fetch_mandatory_article(MANDATORY_REVIEW_TOPIC)
        logger.debug(
            "ThreadResponder: mandatory wiki article loaded ({}, {} chars)",
            article.topic, len(article.text),
        )
        return article.prompt_block()

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
        mr_source_branch: str = "",
        mr_target_branch: str = "",
        ci: CiReading | None = None,
        work_state: str = "",
        linked_notes: Sequence[tuple[str, str]] = (),
    ) -> str:
        parts: list[str] = []
        parts.append("# Review thread context")
        parts.append(f"**MR:** {mr_title}")
        parts.append(f"**URL:** {mr_web_url}")
        parts.append("")
        # Branches come from the GitLab API (see resolve_mr_branches),
        # not from anything a model wrote. They sit above the
        # description on purpose: the description is bot-authored prose
        # that has already, once, claimed a target branch the MR never
        # had, and the responder then defended that claim against
        # humans reading the real MR. Facts first, untrusted text after.
        if mr_source_branch or mr_target_branch or ci is not None:
            parts.append("## Факты о MR (из GitLab — ground truth)")
            if mr_source_branch or mr_target_branch:
                parts.append(
                    f"**Ветки:** `{mr_source_branch or '?'}` → "
                    f"`{mr_target_branch or '?'}`"
                )
            if ci is not None:
                parts.append(f"**CI на последнем коммите:** {_render_ci_fact(ci)}")
            parts.append("")
        if work_state:
            parts.append("## Твоя работа по этому MR (факты рантайма)")
            parts.append(work_state)
            parts.append("")
        if linked_notes:
            # Comment bodies are human-written — untrusted like the thread.
            parts.append("## Комментарии GitLab по ссылкам из треда (прочитаны через API)")
            for url, rendered in linked_notes:
                parts.append(f"### {url}")
                parts.append(self._filter.wrap(
                    rendered, source="gitlab:mr:note",
                ).wrapped_text)
            parts.append("")
        if ci is not None and ci.failed_jobs:
            # Job logs echo code, test names and commit text anyone with
            # push access shapes — untrusted, same as the diff.
            parts.append("## Логи упавших CI-джобов (хвосты)")
            parts.append(self._filter.wrap(
                _render_failed_logs(ci), source="gitlab:ci:logs",
            ).wrapped_text)
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
            render_transcript(thread, self._timezone()), source="mm:thread",
        )
        parts.append(wrapped_thread.wrapped_text)
        parts.append("")
        parts.append("## Latest reply (needs your response)")
        wrapped_latest = self._filter.wrap(
            f"@{latest.author_id} [{local_time(latest.timestamp, self._timezone())}]:\n"
            f"{latest.text}",
            source=f"mm:post:{latest.id}",
        )
        parts.append(wrapped_latest.wrapped_text)
        parts.append("")
        parts.append(
            "Use Read/Glob/Grep if you need to check the actual code before "
            "deciding. When ready, call `submit_response` exactly once."
        )
        return "\n".join(parts)


def _render_ci_fact(ci: CiReading) -> str:
    if ci.state == "failed":
        names = ", ".join(f"[{j.name}]({j.web_url})" for j in ci.failed_jobs)
        return f"красный, упали: {names}"
    if ci.started:
        return (
            "проверки на нём ещё не запускались; ты только что сама "
            f"запустила {', '.join(ci.started)} — идут"
        )
    return {
        "success": "зелёный",
        "running": "идёт",
        "no-ci": "проверок нет",
    }.get(ci.state, "неизвестно (GitLab не ответил)")


def _render_failed_logs(ci: CiReading) -> str:
    return "\n\n".join(
        f"### {j.name}\n{j.log_excerpt or '(лога нет)'}" for j in ci.failed_jobs
    )


async def resolve_mr_branches(
    vcs: VcsPort | None,
    *,
    repo_key: str,
    iid: int,
    fallback_source: str,
    fallback_target: str,
) -> tuple[str, str]:
    """The MR's branches as GitLab has them right now.

    ``MergeRequestRow`` stores the branches once, when the MR is
    opened, and nothing refreshes them. A human retargeting the MR —
    which is exactly what the bot asks for when the ticket wants a
    non-default base — would leave the row lying, and the responder
    renders these as ground truth. So ask GitLab; fall back to the row
    only when the API is unreachable.
    """
    if vcs is None:
        return fallback_source, fallback_target
    try:
        mr = await vcs.get_merge_request(repo_key, iid)
    except Exception:
        logger.exception(
            "resolve_mr_branches: live fetch failed for {}!{}; using stored row",
            repo_key, iid,
        )
        return fallback_source, fallback_target
    return (
        mr.source_branch or fallback_source,
        mr.target_branch or fallback_target,
    )


__all__ = [
    "ThreadResponderAgent",
    "ResponderAction",
    "ResponderDecision",
    "resolve_mr_branches",
]
# Keep json imported for debug-dumps of the decision schema.
_ = json
