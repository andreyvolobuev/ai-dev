"""ThreadResponderAgent — tool surface + activity event tests.

Regression-prone area: ``submit_response.build()`` returns None when
``ToolContext.run_state`` is missing, which silently drops the
terminal tool from the MCP surface. The model then writes a plaintext
answer, ends its turn, the responder logs a WARNING and returns
IGNORE — chat goes silent and the operator has no UI signal.

Both surfaces are covered here:
* ``test_responder_tool_surface_includes_submit_response`` proves the
  ToolContext is wired so the tool actually registers.
* ``test_responder_emits_decision_activity_event`` proves every
  decision (including the "no-submit" failure) surfaces on the
  AgentTrace so the activity tab can render it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from virtual_dev.application.agents.thread_responder import (
    ResponderAction,
    ThreadResponderAgent,
)
from virtual_dev.application.services.agent_trace import AgentTrace
from virtual_dev.application.services.injection_filter import InjectionFilter
from virtual_dev.application.services.prompts import PromptsLoader
from virtual_dev.domain.models.chat import ChatMessage
from virtual_dev.domain.models.wiki_article import WikiArticle
from virtual_dev.domain.ports.code_agent import (
    CodeAgentPort,
    CodeAgentRequest,
    CodeAgentResult,
)
from virtual_dev.domain.ports.professor_wiki import (
    MANDATORY_REVIEW_TOPIC,
    ProfessorWikiPort,
    WikiUnavailableError,
)
from virtual_dev.infrastructure.config import Settings
from virtual_dev.infrastructure.config.schema import (
    AgentsCfg,
    AppConfig,
    MappingsCfg,
)


def _cfg() -> AppConfig:
    return AppConfig(
        repositories=[],
        agents=AgentsCfg(),
        mappings=MappingsCfg(),
    )


class _FakeCodeAgent(CodeAgentPort):
    def __init__(self, captured_to_inject: dict[str, Any] | None = None) -> None:
        self.last_request: CodeAgentRequest | None = None
        self._captured_to_inject = captured_to_inject

    async def run_task(self, request: CodeAgentRequest) -> CodeAgentResult:
        self.last_request = request
        # If the test wired a submit_capture dict on extras, simulate
        # the model calling submit_response by populating it.
        cap = request.extras.get("submit_capture") if request.extras else None
        if isinstance(cap, dict) and self._captured_to_inject is not None:
            cap.update(self._captured_to_inject)
        return CodeAgentResult(
            final_text="", turns=1, input_tokens=0, output_tokens=0,
            cost_usd=0.01, stopped_reason="end_turn",
        )

    def stream_task(self, request: CodeAgentRequest) -> AsyncIterator[str]:  # pragma: no cover
        async def _empty() -> AsyncIterator[str]:
            if False:
                yield ""
        return _empty()


def _msg(*, id: str, author: str, text: str, root: str = "root-x") -> ChatMessage:
    return ChatMessage(
        id=id, channel_id="c", author_id=author, text=text,
        timestamp=datetime.now(timezone.utc), thread_root_id=root,
        trusted=False,
    )


@pytest.mark.asyncio
async def test_responder_tool_surface_includes_submit_response() -> None:
    """Regression: submit_response must register on the responder's
    MCP surface. If ToolContext is missing ``run_state``,
    ``submit_response.build()`` returns None and the tool silently
    drops — the model never sees it and ends the turn with plaintext.

    Asserts the kwarg the responder hands to the SDK includes the
    fully-qualified MCP tool name."""
    fake = _FakeCodeAgent()
    responder = ThreadResponderAgent(
        code_agent=fake,
        config=_cfg(),
        prompts_loader=PromptsLoader("/no-prompts-dir"),
        injection_filter=InjectionFilter(),
    )

    await responder.decide(
        mr_title="t", mr_description="d", mr_web_url="u",
        plan=None, thread=[], latest_reply=_msg(id="p", author="a", text="?"),
    )

    assert fake.last_request is not None
    allowed = cast(
        list[str],
        fake.last_request.extras.get("allowed_tool_names") or [],
    )
    assert "mcp__virtual_dev_responder__submit_response" in allowed, (
        f"submit_response missing from MCP allow-list: {allowed}"
    )


@pytest.mark.asyncio
async def test_responder_tool_surface_adds_configured_professor_wiki() -> None:
    """The review-decision call gets the remote wiki tools when the
    Professor MCP connection is configured (and none when it is not)."""
    import os
    from unittest import mock

    from virtual_dev.tools import (
        PROFESSOR_SERVER_KEY,
        PROFESSOR_WIKI_GET_PAGE_TOOL,
        PROFESSOR_WIKI_SEARCH_TOOL,
    )

    fake = _FakeCodeAgent()
    fake_env = {
        "PROFESSOR_MCP_URL": "https://professor.example/mcp",
        "PROFESSOR_MCP_TOKEN": "prof-token",
    }
    with mock.patch.dict(os.environ, fake_env, clear=False):
        settings = Settings()

    responder = ThreadResponderAgent(
        code_agent=fake,
        config=_cfg(),
        prompts_loader=PromptsLoader("/no-prompts-dir"),
        injection_filter=InjectionFilter(),
        settings=settings,
    )

    await responder.decide(
        mr_title="t", mr_description="d", mr_web_url="u",
        plan=None, thread=[], latest_reply=_msg(id="p", author="a", text="?"),
    )

    assert fake.last_request is not None
    servers = cast(
        dict[str, Any],
        fake.last_request.extras.get("mcp_servers") or {},
    )
    allowed = cast(
        list[str],
        fake.last_request.extras.get("allowed_tool_names") or [],
    )
    assert PROFESSOR_SERVER_KEY in servers
    assert servers[PROFESSOR_SERVER_KEY]["type"] == "http"
    assert PROFESSOR_WIKI_SEARCH_TOOL in allowed
    assert PROFESSOR_WIKI_GET_PAGE_TOOL in allowed
    assert "mcp__virtual_dev_responder__submit_response" in allowed


@pytest.mark.asyncio
async def test_responder_tool_surface_stays_local_without_professor() -> None:
    """No Professor settings = byte-compatible old surface."""
    import os
    from unittest import mock

    from virtual_dev.tools import PROFESSOR_SERVER_KEY

    fake = _FakeCodeAgent()
    with mock.patch.dict(
        os.environ,
        {"PROFESSOR_MCP_URL": "", "PROFESSOR_MCP_TOKEN": ""},
        clear=False,
    ):
        settings = Settings()

    responder = ThreadResponderAgent(
        code_agent=fake,
        config=_cfg(),
        prompts_loader=PromptsLoader("/no-prompts-dir"),
        injection_filter=InjectionFilter(),
        settings=settings,
    )

    await responder.decide(
        mr_title="t", mr_description="d", mr_web_url="u",
        plan=None, thread=[], latest_reply=_msg(id="p", author="a", text="?"),
    )

    assert fake.last_request is not None
    servers = cast(
        dict[str, Any],
        fake.last_request.extras.get("mcp_servers") or {},
    )
    allowed = cast(
        list[str],
        fake.last_request.extras.get("allowed_tool_names") or [],
    )
    assert PROFESSOR_SERVER_KEY not in servers
    assert "mcp__virtual_dev_responder__submit_response" in allowed


@pytest.mark.asyncio
async def test_responder_emits_decision_activity_event_on_no_submit() -> None:
    """When the model skips submit_response (text-only turn) the
    responder currently returns IGNORE silently. Operators have no
    way to tell from the live UI that a comment was processed but
    the bot decided not to act — they assume it's broken. The
    activity tab must show every decision, including the failure
    case where reasoning='model-did-not-submit'."""
    fake = _FakeCodeAgent(captured_to_inject=None)   # no submit
    trace = AgentTrace()
    responder = ThreadResponderAgent(
        code_agent=fake,
        config=_cfg(),
        prompts_loader=PromptsLoader("/no-prompts-dir"),
        injection_filter=InjectionFilter(),
        trace=trace,
    )

    decision = await responder.decide(
        mr_title="DM-1: x", mr_description="", mr_web_url="https://gitlab/x/1",
        plan=None, thread=[], latest_reply=_msg(id="p", author="alice", text="?"),
    )

    assert decision.action == ResponderAction.IGNORE
    events = [e for e in list(trace._history) if e.type == "responder_decision"]
    assert len(events) == 1, (
        f"expected exactly one responder_decision event; "
        f"got types={[e.type for e in trace._history]}"
    )
    e = events[0]
    assert e.payload.get("action") == "ignore"
    assert e.payload.get("reasoning") == "model-did-not-submit"


@pytest.mark.asyncio
async def test_responder_emits_decision_activity_event_on_reply() -> None:
    """A normal reply decision must also surface on the activity tab
    so the operator can scan the responder's recent decisions
    (chat-only replies, iterations, push-backs) at a glance."""
    fake = _FakeCodeAgent(captured_to_inject={
        "action": "reply",
        "reply_text": "вот объяснение",
        "reasoning": "answers-the-question",
    })
    trace = AgentTrace()
    responder = ThreadResponderAgent(
        code_agent=fake,
        config=_cfg(),
        prompts_loader=PromptsLoader("/no-prompts-dir"),
        injection_filter=InjectionFilter(),
        trace=trace,
    )

    decision = await responder.decide(
        mr_title="DM-1: x", mr_description="", mr_web_url="https://gitlab/x/1",
        plan=None, thread=[], latest_reply=_msg(id="p", author="alice", text="?"),
    )

    assert decision.action == ResponderAction.REPLY
    events = [e for e in list(trace._history) if e.type == "responder_decision"]
    assert len(events) == 1
    e = events[0]
    assert e.payload.get("action") == "reply"
    assert e.payload.get("reply_text") == "вот объяснение"
    assert e.payload.get("reasoning") == "answers-the-question"


@pytest.mark.asyncio
async def test_prompt_carries_real_branches_as_verified_facts() -> None:
    """Regression: the responder told humans the MR targeted `tags-dev`
    and defended it three times. Its only branch "knowledge" was the
    bot-written MR description, which the prompt itself labels
    untrusted. The real branches come from GitLab via the MR row and
    must be rendered as facts the model may not contradict."""
    fake = _FakeCodeAgent()
    responder = ThreadResponderAgent(
        code_agent=fake,
        config=_cfg(),
        prompts_loader=PromptsLoader("/no-prompts-dir"),
        injection_filter=InjectionFilter(),
    )

    await responder.decide(
        mr_title="t",
        mr_description="Target branch: tags-dev.",
        mr_web_url="u",
        mr_source_branch="ai-dev/dm-2911-back",
        mr_target_branch="master",
        plan=None, thread=[], latest_reply=_msg(id="p", author="a", text="куда льём?"),
    )

    assert fake.last_request is not None
    prompt = fake.last_request.user_prompt
    facts = prompt.split("## MR description", 1)[0]
    # Branches live ABOVE the untrusted description, in the facts block.
    assert "ai-dev/dm-2911-back" in facts
    assert "master" in facts


@pytest.mark.asyncio
async def test_prompt_carries_ci_state_and_failed_logs() -> None:
    """Asked «CI упал, глянь», the bot replied «скинь ссылку на красную
    джобу» — it had no CI in its prompt. Red jobs and their log tails
    are facts now."""
    from virtual_dev.application.services.ci_state import CiReading
    from virtual_dev.domain.models.merge_request import PipelineJob

    fake = _FakeCodeAgent()
    responder = ThreadResponderAgent(
        code_agent=fake, config=_cfg(),
        prompts_loader=PromptsLoader("/no-prompts-dir"),
        injection_filter=InjectionFilter(),
    )
    mypy = PipelineJob(
        id=1, name="01:rapid/mypy", stage="check", status="failed",
        web_url="https://gitlab/x/-/jobs/1",
        log_excerpt="api.py:12: error: Incompatible return value type",
    )

    await responder.decide(
        mr_title="t", mr_description="", mr_web_url="u",
        plan=None, thread=[],
        latest_reply=_msg(id="p", author="a", text="там CI упал"),
        ci=CiReading(state="failed", jobs=[mypy]),
    )

    assert fake.last_request is not None
    prompt = fake.last_request.user_prompt
    facts = prompt.split("## MR description", 1)[0]
    assert "красный" in facts
    assert "01:rapid/mypy" in facts
    assert "Incompatible return value type" in prompt


def test_responder_prompt_ranks_gitlab_facts_above_prose() -> None:
    prompt = (
        Path(__file__).parents[2] / "config" / "prompts" / "thread_responder.md"
    ).read_text("utf-8")
    assert "## Факты о MR" in prompt


@pytest.mark.asyncio
async def test_responder_reads_links_knows_its_work_and_can_attach_files(
    tmp_path: Path,
) -> None:
    """rainbow!1031: the responder asked a human to paste a linked GitLab
    comment, answered «в чём затык?» from its own chat lines, quoted UTC
    times, said it can't attach a file and never saw the repo's rules."""
    from virtual_dev.infrastructure.config import Settings

    (tmp_path / "AGENTS.md").write_text("Ветку пересобирают по `docs/rebuild.md`.\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "rebuild.md").write_text("ШАГИ ПЕРЕСБОРКИ\n")
    fake = _FakeCodeAgent(captured_to_inject={
        "action": "reply", "reply_text": "Отчёт во вложении.", "reasoning": "r",
        "attachment_name": "report.md", "attachment_content": "# Отчёт",
    })
    responder = ThreadResponderAgent(
        code_agent=fake, config=_cfg(),
        prompts_loader=PromptsLoader("/no-prompts-dir"),
        injection_filter=InjectionFilter(), settings=Settings(),
    )
    noon_utc = datetime(2026, 11, 3, 11, 20, tzinfo=timezone.utc)
    earlier = ChatMessage(
        id="e", channel_id="c", author_id="e.yakovlev", text="ок!",
        timestamp=noon_utc, thread_root_id="root-x", trusted=False,
    )

    decision = await responder.decide(
        mr_title="t", mr_description="d", mr_web_url="u", plan=None,
        thread=[earlier], latest_reply=_msg(id="p", author="a", text="в чём затык?"),
        repo_workspace=str(tmp_path),
        work_state="- Последняя правка: не вышло: GitLab не принял пуш",
        linked_notes=[("https://g/-/merge_requests/1#note_5", "→ @e.yakovlev: тест не нужен")],
    )

    request = fake.last_request
    assert request is not None
    assert "не вышло: GitLab не принял пуш" in request.user_prompt
    assert "тест не нужен" in request.user_prompt
    assert "[2026-11-03 14:20]" in request.user_prompt
    assert "ШАГИ ПЕРЕСБОРКИ" in request.system_prompt
    allowed = request.extras["allowed_tool_names"]
    assert "mcp__virtual_dev_shared__fetch_url" in allowed
    assert not any(
        name.startswith("mcp__virtual_dev_shared__")
        and name.rsplit("__", 1)[1] not in ("fetch_url", "read_merge_request")
        for name in allowed
    )
    assert decision.attachment == ("report.md", "# Отчёт")


class _WikiStub(ProfessorWikiPort):
    """Стаб обязательной статьи: пишет темы вызовов, отдаёт статью/ошибку."""

    def __init__(
        self,
        article: WikiArticle | None = None,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._article = article
        self._error = error

    async def fetch_mandatory_article(self, topic: str) -> WikiArticle:
        self.calls.append(topic)
        if self._error is not None:
            raise self._error
        assert self._article is not None
        return self._article


_MANDATORY_TEXT = (
    '<untrusted_content source="lotsiya:krusenstern/chris-review">\n'
    "Лоция — статья «Chris Review» (тема: krusenstern/chris-review).\n"
    "Статус: проверена 2026-11-02.\n"
    "## Правила ревью\n- честность\n"
    "</untrusted_content>"
)


def _wiki_stub(error: Exception | None = None) -> _WikiStub:
    article = None if error else WikiArticle(topic=MANDATORY_REVIEW_TOPIC, text=_MANDATORY_TEXT)
    return _WikiStub(article=article, error=error)


def _responder(fake: _FakeCodeAgent, wiki: ProfessorWikiPort | None) -> ThreadResponderAgent:
    return ThreadResponderAgent(
        code_agent=fake,
        config=_cfg(),
        prompts_loader=PromptsLoader("/no-prompts-dir"),
        injection_filter=InjectionFilter(),
        wiki=wiki,
    )


@pytest.mark.asyncio
async def test_responder_injects_mandatory_article_into_system_prompt() -> None:
    """Реальное решение получает полный текст стандарта ревью в
    system prompt — независимо от того, вызовет ли модель wiki-инструменты."""
    fake = _FakeCodeAgent()
    wiki = _wiki_stub()
    responder = _responder(fake, wiki)

    decision = await responder.decide(
        mr_title="t", mr_description="d", mr_web_url="u",
        plan=None, thread=[], latest_reply=_msg(id="p", author="a", text="ок?"),
    )

    assert decision.action == ResponderAction.IGNORE  # модель-фейк не сабмитит
    assert wiki.calls == [MANDATORY_REVIEW_TOPIC]
    assert fake.last_request is not None
    system_prompt = fake.last_request.system_prompt
    assert "<mandatory_wiki_context>" in system_prompt
    assert _MANDATORY_TEXT in system_prompt
    assert system_prompt.rstrip().endswith("</mandatory_wiki_context>")


@pytest.mark.asyncio
async def test_responder_wiki_outage_blocks_decision_before_model() -> None:
    """Недоступность Wiki блокирует решение: модель не запускается,
    WikiUnavailableError уходит наружу — вызывающий не сделает ack."""
    fake = _FakeCodeAgent()
    wiki = _wiki_stub(error=WikiUnavailableError("wiki down"))
    responder = _responder(fake, wiki)

    with pytest.raises(WikiUnavailableError):
        await responder.decide(
            mr_title="t", mr_description="d", mr_web_url="u",
            plan=None, thread=[], latest_reply=_msg(id="p", author="a", text="ок?"),
        )

    assert fake.last_request is None  # модель не стартовала


@pytest.mark.asyncio
async def test_responder_without_wiki_has_no_mandatory_block() -> None:
    """wiki=None (интеграция выключена) — решение работает как раньше,
    без обязательного блока."""
    fake = _FakeCodeAgent()
    responder = _responder(fake, None)

    await responder.decide(
        mr_title="t", mr_description="d", mr_web_url="u",
        plan=None, thread=[], latest_reply=_msg(id="p", author="a", text="ок?"),
    )

    assert fake.last_request is not None
    assert "<mandatory_wiki_context>" not in fake.last_request.system_prompt
