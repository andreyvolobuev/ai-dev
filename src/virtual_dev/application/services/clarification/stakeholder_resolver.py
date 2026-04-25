"""StakeholderResolver — turns a raw ``ask_whom`` hint into a Stakeholder.

Resolution precedence:

1. Empty / whitespace → fall back to ``UNRESOLVED_NAME`` (orchestrator
   will route the question to the team-lead via ``TEAM_LEAD`` kind).
2. ``@nick`` or bare ``user.name`` → MM ``find_user_by_username``.
3. Looks like email → MM ``find_user_by_email``.
4. Free-form name ("Вася Курочкин", "the platform team") → LLM
   normalisation. The LLM tries to extract a candidate handle / email
   from the raw_hint. If it returns one with confidence ≥ threshold, we
   verify it exists in MM. If not, the resolver returns
   ``UNRESOLVED_NAME`` and the orchestrator will spawn an
   ``ASKING_FOR_STAKEHOLDER`` child to ask the original respondent.

The LLM validation step is deliberate. Per the project rule, **every**
clarification path that isn't trivially deterministic goes through an
LLM check — the user explicitly forbade regex-only heuristics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig  # type: ignore[attr-defined]
from loguru import logger

from virtual_dev.application.services.communicator import CommunicatorService
from virtual_dev.application.services.injection_filter import (
    SYSTEM_PROMPT_ABOUT_UNTRUSTED,
    InjectionFilter,
)
from virtual_dev.application.services.prompts import PromptsLoader
from virtual_dev.domain.models.clarification import Stakeholder, StakeholderKind
from virtual_dev.domain.ports.code_agent import CodeAgentPort, CodeAgentRequest
from virtual_dev.infrastructure.config import AppConfig


_PROMPT_NAME = "stakeholder_resolver"
_FALLBACK_PROMPT = (
    "You are the Stakeholder Resolver. Given a free-form mention of a "
    "person/team, propose a likely Mattermost handle or email. "
    "Call submit_resolution exactly once.\n\n"
    "{untrusted_warning}"
)


_SUBMIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["use_handle", "use_email", "give_up"],
        },
        "handle": {"type": ["string", "null"]},
        "email": {"type": ["string", "null"]},
        "display_name": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": ["action", "confidence", "reasoning"],
}


_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@dataclass
class ResolveContext:
    """What the resolver knows when resolving a hint.

    For Phase 3.8 this is mostly empty — we just have the raw hint and
    the issue's metadata. Phase 4 will add MM user-search results so
    the LLM can pick from a candidate list rather than guessing.
    """

    issue_summary: str = ""


class StakeholderResolver:
    agent_key = "stakeholder-resolver"

    def __init__(
        self,
        *,
        communicator: CommunicatorService,
        code_agent: CodeAgentPort,
        config: AppConfig,
        prompts_loader: PromptsLoader,
        injection_filter: InjectionFilter | None = None,
        confidence_threshold: float = 0.8,
        max_turns: int = 6,
    ) -> None:
        self._communicator = communicator
        self._code_agent = code_agent
        self._config = config
        self._prompts = prompts_loader
        self._filter = injection_filter or InjectionFilter()
        self._confidence_threshold = confidence_threshold
        self._max_turns = max_turns

    async def resolve(
        self,
        raw_hint: str,
        context: ResolveContext | None = None,
    ) -> Stakeholder:
        hint = (raw_hint or "").strip().lstrip("@").strip()
        if not hint:
            return Stakeholder(kind=StakeholderKind.UNRESOLVED_NAME, raw_hint="")

        # 1. Email shape — deterministic.
        if _EMAIL_RE.match(hint):
            user_id = await self._communicator.resolve_user_id(email=hint)
            if user_id is not None:
                return Stakeholder(
                    kind=StakeholderKind.EMAIL,
                    raw_hint=raw_hint,
                    resolved_mm_user_id=user_id,
                    display_name=hint,
                )

        # 2. Username shape — deterministic.
        if _USERNAME_RE.match(hint):
            user_id = await self._communicator.resolve_user_id(username=hint)
            if user_id is not None:
                return Stakeholder(
                    kind=StakeholderKind.EXPLICIT_HANDLE,
                    raw_hint=raw_hint,
                    resolved_mm_user_id=user_id,
                    display_name=hint,
                )

        # 3. Free-form / unresolved deterministic — try the LLM.
        try:
            resolved = await self._llm_normalise(
                raw_hint, context or ResolveContext(),
            )
        except Exception:
            logger.exception(
                "StakeholderResolver: LLM normalisation crashed for {!r}", raw_hint,
            )
            return Stakeholder(kind=StakeholderKind.UNRESOLVED_NAME, raw_hint=raw_hint)

        return resolved

    async def _llm_normalise(
        self, raw_hint: str, context: ResolveContext,
    ) -> Stakeholder:
        captured: dict[str, Any] = {}

        @tool(
            "submit_resolution",
            "Submit your guess for who this hint refers to. Call exactly once.",
            _SUBMIT_SCHEMA,
        )
        async def _submit(args: dict[str, Any]) -> dict[str, Any]:
            captured.clear()
            captured.update(args)
            return {"content": [{"type": "text", "text": "Recorded."}]}

        server = create_sdk_mcp_server(
            name="virtual_dev_stakeholder_resolver", version="0.1.0",
            tools=[_submit],
        )
        mcp_servers: dict[str, McpSdkServerConfig] = {
            "virtual_dev_stakeholder_resolver": server,
        }
        allowed = ["mcp__virtual_dev_stakeholder_resolver__submit_resolution"]

        prompt_parts: list[str] = []
        prompt_parts.append("# Resolve a stakeholder")
        prompt_parts.append("")
        prompt_parts.append("## Raw hint (untrusted)")
        wrapped = self._filter.wrap(raw_hint, source="analyst:ask_whom")
        prompt_parts.append(wrapped.wrapped_text)
        if context.issue_summary.strip():
            prompt_parts.append("")
            prompt_parts.append("## Issue context")
            prompt_parts.append(context.issue_summary.strip())
        prompt_parts.append("")
        prompt_parts.append(
            "Propose a likely Mattermost handle (e.g. `vasya.kurochkin` "
            "or `@vasya`) OR a likely corporate email if the hint "
            "looks like a name. If unsure, choose `give_up`."
        )
        prompt_parts.append("")
        prompt_parts.append("Call `submit_resolution` exactly once.")

        request = CodeAgentRequest(
            agent_key=self.agent_key,
            system_prompt=self._prompts.render(
                _PROMPT_NAME,
                fallback=_FALLBACK_PROMPT,
                untrusted_warning=SYSTEM_PROMPT_ABOUT_UNTRUSTED,
            ),
            user_prompt="\n".join(prompt_parts),
            working_dir=None,
            max_turns=self._max_turns,
            model=self._resolve_model(),
        )
        request.extras["mcp_servers"] = mcp_servers
        request.extras["allowed_tool_names"] = allowed
        await self._code_agent.run_task(request)

        if not captured:
            return Stakeholder(kind=StakeholderKind.UNRESOLVED_NAME, raw_hint=raw_hint)

        try:
            confidence = float(captured.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0

        action = str(captured.get("action") or "give_up").lower()
        if confidence < self._confidence_threshold or action == "give_up":
            return Stakeholder(
                kind=StakeholderKind.UNRESOLVED_NAME, raw_hint=raw_hint,
                display_name=str(captured.get("display_name") or "") or None,
            )

        if action == "use_handle":
            handle = str(captured.get("handle") or "").strip().lstrip("@")
            if handle and _USERNAME_RE.match(handle):
                user_id = await self._communicator.resolve_user_id(username=handle)
                if user_id is not None:
                    return Stakeholder(
                        kind=StakeholderKind.EXPLICIT_HANDLE,
                        raw_hint=raw_hint,
                        resolved_mm_user_id=user_id,
                        display_name=(
                            str(captured.get("display_name") or "") or handle
                        ),
                    )
        elif action == "use_email":
            email = str(captured.get("email") or "").strip()
            if email and _EMAIL_RE.match(email):
                user_id = await self._communicator.resolve_user_id(email=email)
                if user_id is not None:
                    return Stakeholder(
                        kind=StakeholderKind.EMAIL,
                        raw_hint=raw_hint,
                        resolved_mm_user_id=user_id,
                        display_name=(
                            str(captured.get("display_name") or "") or email
                        ),
                    )

        # LLM proposed something but MM didn't recognise it.
        return Stakeholder(
            kind=StakeholderKind.UNRESOLVED_NAME, raw_hint=raw_hint,
            display_name=str(captured.get("display_name") or "") or None,
        )

    def _resolve_model(self) -> str:
        agent_cfg = self._config.agents.agents.get(self.agent_key.replace("-", "_"))
        if agent_cfg is None:
            return self._config.agents.models.lightweight
        chosen = agent_cfg.model or "lightweight"
        return getattr(
            self._config.agents.models, chosen,
            self._config.agents.models.lightweight,
        )


__all__ = ["ResolveContext", "StakeholderResolver"]
