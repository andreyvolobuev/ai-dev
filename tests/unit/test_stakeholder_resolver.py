"""StakeholderResolver — explicit/email paths + LLM fallback."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timezone
from typing import Any

import pytest

from virtual_dev.application.services import (
    CommunicatorService,
    InjectionFilter,
    PromptsLoader,
)
from virtual_dev.application.services.clarification.stakeholder_resolver import (
    ResolveContext,
    StakeholderResolver,
)
from virtual_dev.domain.models.chat import ChatMessage, ChatUser
from virtual_dev.domain.models.clarification import StakeholderKind
from virtual_dev.domain.ports.chat import ChatPort
from virtual_dev.domain.ports.code_agent import (
    CodeAgentPort,
    CodeAgentRequest,
    CodeAgentResult,
)
from virtual_dev.infrastructure.config import (
    AgentsCfg,
    AppConfig,
    MappingsCfg,
    NotificationsCfg,
    RepositoryCfg,
)


class _RecordingChat(ChatPort):
    def __init__(self) -> None:
        self.users_by_username: dict[str, ChatUser] = {
            "alice": ChatUser(id="uid-alice", username="alice"),
            "vasya.kurochkin": ChatUser(id="uid-vasya", username="vasya.kurochkin"),
        }
        self.users_by_email: dict[str, ChatUser] = {
            "bob@2gis.ru": ChatUser(id="uid-bob", username="bob", email="bob@2gis.ru"),
        }

    async def find_user_by_email(self, email: str) -> ChatUser | None:
        return self.users_by_email.get(email)

    async def find_user_by_username(self, username: str) -> ChatUser | None:
        return self.users_by_username.get(username)

    async def read_thread(self, thread_root_id: str) -> Sequence[ChatMessage]:
        return []

    async def send_direct(self, user_id: str, text: str) -> ChatMessage:
        raise NotImplementedError

    async def send_to_channel(
        self, channel_id: str, text: str, thread_root_id: str | None = None,
    ) -> ChatMessage:
        raise NotImplementedError

    async def add_reaction(self, post_id: str, emoji_name: str) -> None:
        return None

    async def get_post(self, post_id: str) -> ChatMessage | None:
        return None

    def subscribe(self) -> AsyncIterator[ChatMessage]:
        raise NotImplementedError


class _StubCodeAgent(CodeAgentPort):
    def __init__(self, result: CodeAgentResult) -> None:
        self.result = result

    async def run_task(self, request: CodeAgentRequest) -> CodeAgentResult:
        return self.result

    def stream_task(self, request: CodeAgentRequest) -> AsyncIterator[str]:  # pragma: no cover
        raise NotImplementedError


class _PreseedResolver(StakeholderResolver):
    """Resolver with ``_llm_normalise`` overridden to return canned data.

    We don't actually want the test to hit the SDK — we just check the
    branch logic.
    """

    def __init__(self, *args: Any, captured: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._captured = captured

    async def _llm_normalise(self, raw_hint, context):  # type: ignore[no-untyped-def]
        # Recreate the post-LLM logic by hand using captured payload.
        from virtual_dev.application.services.clarification.stakeholder_resolver import (
            _EMAIL_RE,
            _USERNAME_RE,
        )
        from virtual_dev.domain.models.clarification import (
            Stakeholder,
            StakeholderKind,
        )

        action = str(self._captured.get("action") or "give_up").lower()
        try:
            confidence = float(self._captured.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if action == "give_up" or confidence < self._confidence_threshold:
            return Stakeholder(
                kind=StakeholderKind.UNRESOLVED_NAME,
                raw_hint=raw_hint,
                display_name=str(self._captured.get("display_name") or "") or None,
            )

        if action == "use_handle":
            handle = str(self._captured.get("handle") or "").strip().lstrip("@")
            if handle and _USERNAME_RE.match(handle):
                user_id = await self._communicator.resolve_user_id(username=handle)
                if user_id:
                    return Stakeholder(
                        kind=StakeholderKind.EXPLICIT_HANDLE,
                        raw_hint=raw_hint,
                        resolved_mm_user_id=user_id,
                        display_name=str(
                            self._captured.get("display_name") or handle,
                        ),
                    )
        elif action == "use_email":
            email = str(self._captured.get("email") or "").strip()
            if email and _EMAIL_RE.match(email):
                user_id = await self._communicator.resolve_user_id(email=email)
                if user_id:
                    return Stakeholder(
                        kind=StakeholderKind.EMAIL,
                        raw_hint=raw_hint,
                        resolved_mm_user_id=user_id,
                        display_name=str(
                            self._captured.get("display_name") or email,
                        ),
                    )

        return Stakeholder(
            kind=StakeholderKind.UNRESOLVED_NAME, raw_hint=raw_hint,
        )


def _resolver(captured: dict[str, Any]) -> _PreseedResolver:
    chat = _RecordingChat()
    cfg = AppConfig(
        repositories=[RepositoryCfg(key="x", url="git@x:x.git")],
        agents=AgentsCfg(),
        mappings=MappingsCfg(),
        notifications=NotificationsCfg(),
    )
    return _PreseedResolver(
        communicator=CommunicatorService(
            chat, InjectionFilter(), respect_working_hours=False,
        ),
        code_agent=_StubCodeAgent(CodeAgentResult(
            final_text="", turns=0, input_tokens=0, output_tokens=0,
            cost_usd=0.0, stopped_reason="end_turn",
        )),
        config=cfg,
        prompts_loader=PromptsLoader("config/prompts"),
        injection_filter=InjectionFilter(),
        captured=captured,
    )


@pytest.mark.asyncio
async def test_explicit_handle_resolves_directly_no_llm() -> None:
    r = _resolver(captured={})  # LLM never called
    out = await r.resolve("alice")
    assert out.kind is StakeholderKind.EXPLICIT_HANDLE
    assert out.resolved_mm_user_id == "uid-alice"


@pytest.mark.asyncio
async def test_email_resolves_directly_no_llm() -> None:
    r = _resolver(captured={})
    out = await r.resolve("bob@2gis.ru")
    assert out.kind is StakeholderKind.EMAIL
    assert out.resolved_mm_user_id == "uid-bob"


@pytest.mark.asyncio
async def test_free_form_name_uses_llm_to_propose_handle() -> None:
    r = _resolver(captured={
        "action": "use_handle",
        "handle": "vasya.kurochkin",
        "display_name": "Вася Курочкин",
        "confidence": 0.85,
    })
    out = await r.resolve("Вася Курочкин")
    assert out.kind is StakeholderKind.EXPLICIT_HANDLE
    assert out.resolved_mm_user_id == "uid-vasya"
    assert out.display_name == "Вася Курочкин"


@pytest.mark.asyncio
async def test_low_confidence_returns_unresolved_name() -> None:
    r = _resolver(captured={
        "action": "use_handle",
        "handle": "vasya.kurochkin",
        "confidence": 0.5,
    })
    out = await r.resolve("Вася как-то-там")
    assert out.kind is StakeholderKind.UNRESOLVED_NAME


@pytest.mark.asyncio
async def test_give_up_returns_unresolved_name() -> None:
    r = _resolver(captured={
        "action": "give_up", "confidence": 0.95, "reasoning": "no idea",
    })
    out = await r.resolve("the platform team")
    assert out.kind is StakeholderKind.UNRESOLVED_NAME


@pytest.mark.asyncio
async def test_llm_proposes_handle_but_mm_doesnt_recognise() -> None:
    r = _resolver(captured={
        "action": "use_handle",
        "handle": "ghost.user",
        "confidence": 0.9,
    })
    # ghost.user not in our chat fake → MM lookup returns None → unresolved.
    out = await r.resolve("кто-то-там")
    assert out.kind is StakeholderKind.UNRESOLVED_NAME
