"""Тесты MCP-адаптера обязательной статьи Профессора (Этап 2)."""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from virtual_dev.adapters.knowledge_base.professor_wiki import (
    ProfessorWikiMcp,
    build_initialize_payload,
    professor_wiki_from_settings,
    build_initialized_notification,
    build_tools_call_payload,
    extract_tool_text,
    parse_article,
    parse_initialize_protocol,
    parse_session_id,
    untrusted_open_tag,
)
from virtual_dev.domain.models.wiki_article import WikiArticle
from virtual_dev.domain.ports.professor_wiki import WikiUnavailableError
from virtual_dev.infrastructure.config import Settings

TOPIC = "krusenstern/chris-review"
TOKEN = "super-secret-token-XYZ"
URL = "https://prof.example/mcp"


def wrap_article(topic: str, title: str, content: str, notes_block: str | None = None) -> str:
    """Собрать текст ответа wiki_get_page в форме, которую шлёт Профессор."""
    body = f"Лоция — статья «{title}» (тема: {topic}).\nСтатус: проверена 2026-11-02.\n{content}"
    prefix = f"{notes_block}\n" if notes_block else ""
    return f'{prefix}<untrusted_content source="lotsiya:{topic}">\n{body}\n</untrusted_content>'


def not_found_text(topic: str) -> str:
    """Форма «не найдено»: isError=false, текст-подсказка без обёртки."""
    return (
        f"Тема {topic!r} в Лоции не найдена или недоступна этой аудитории. "
        "Доступные темы: other/topic."
    )


def rpc_result(result: dict[str, Any], *, session_id: str | None = None) -> httpx.Response:
    headers = {"mcp-session-id": session_id} if session_id else {}
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result}, headers=headers)


def tool_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class ScriptedHandler:
    """Скриптованный транспорт: ответы по порядку + журнал запросов."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responses.pop(0)


def make_adapter(
    handler: Callable[[httpx.Request], httpx.Response],
) -> ProfessorWikiMcp:
    return ProfessorWikiMcp(url=URL, token=TOKEN, transport=httpx.MockTransport(handler))


def success_responses(tool_result_body: dict[str, Any]) -> list[httpx.Response]:
    return [
        rpc_result({"protocolVersion": "2025-06-18"}, session_id="sess-1"),
        httpx.Response(202),
        rpc_result(tool_result_body),
    ]


# --- Чистые хелперы -------------------------------------------------------


def test_ctor_rejects_empty_url_and_token() -> None:
    with pytest.raises(ValueError, match="url"):
        ProfessorWikiMcp(url="", token=TOKEN)
    with pytest.raises(ValueError, match="token"):
        ProfessorWikiMcp(url=URL, token="")


def test_build_initialize_payload_shape() -> None:
    payload = build_initialize_payload()
    assert payload["method"] == "initialize"
    assert payload["id"] == 1
    assert payload["params"]["protocolVersion"] == "2025-06-18"
    assert payload["params"]["clientInfo"]["name"] == "virtual-dev"


def test_build_initialized_notification_has_no_id() -> None:
    payload = build_initialized_notification()
    assert payload["method"] == "notifications/initialized"
    assert "id" not in payload


def test_build_tools_call_payload_shape() -> None:
    payload = build_tools_call_payload(2, TOPIC)
    assert payload["method"] == "tools/call"
    assert payload["params"] == {"name": "wiki_get_page", "arguments": {"topic": TOPIC}}


def test_untrusted_open_tag_exact() -> None:
    assert untrusted_open_tag(TOPIC) == f'<untrusted_content source="lotsiya:{TOPIC}">'


def test_parse_session_id_missing_raises() -> None:
    with pytest.raises(WikiUnavailableError, match="mcp-session-id"):
        parse_session_id(httpx.Response(200))


def test_parse_session_id_present() -> None:
    response = httpx.Response(200, headers={"mcp-session-id": "sess-9"})
    assert parse_session_id(response) == "sess-9"


def test_parse_initialize_protocol_echo_and_fallback() -> None:
    echoed = httpx.Response(200, json={"result": {"protocolVersion": "2025-03-26"}})
    assert parse_initialize_protocol(echoed, "2025-06-18") == "2025-03-26"
    no_echo = httpx.Response(200, json={"result": {}})
    assert parse_initialize_protocol(no_echo, "2025-06-18") == "2025-06-18"


def test_extract_tool_text_malformed_json_raises() -> None:
    with pytest.raises(WikiUnavailableError, match="JSON"):
        extract_tool_text(httpx.Response(200, text="not json"))


def test_extract_tool_text_json_rpc_error_raises() -> None:
    body = {"jsonrpc": "2.0", "id": 2, "error": {"code": -32601, "message": "no such tool"}}
    with pytest.raises(WikiUnavailableError, match="JSON-RPC"):
        extract_tool_text(httpx.Response(200, json=body))


def test_extract_tool_text_is_error_raises() -> None:
    body = {"result": tool_result("boom", is_error=True)}
    with pytest.raises(WikiUnavailableError, match="isError"):
        extract_tool_text(httpx.Response(200, json=body))


def test_extract_tool_text_empty_content_raises() -> None:
    body = {"result": {"content": [], "isError": False}}
    with pytest.raises(WikiUnavailableError, match="пустой текст"):
        extract_tool_text(httpx.Response(200, json=body))


def test_extract_tool_text_plain_text_part() -> None:
    body = {"result": tool_result("hello")}
    assert extract_tool_text(httpx.Response(200, json=body)) == "hello"


def test_parse_article_success_with_notes_block() -> None:
    text = wrap_article(TOPIC, "Chris Review", "## Правила\n- честность", notes_block="[warn]")
    article = parse_article(TOPIC, text)
    assert isinstance(article, WikiArticle)
    assert article.topic == TOPIC
    assert article.text == text  # текст доставляется без изменений


def test_parse_article_missing_wrapper_raises() -> None:
    with pytest.raises(WikiUnavailableError, match="не получена"):
        parse_article(TOPIC, not_found_text(TOPIC))


def test_parse_article_topic_mismatch_raises() -> None:
    text = wrap_article("other/topic", "Другая", "текст")
    with pytest.raises(WikiUnavailableError, match="не получена"):
        parse_article(TOPIC, text)


def test_parse_article_missing_close_tag_raises() -> None:
    text = f'<untrusted_content source="lotsiya:{TOPIC}">\nЗаголовок\nСтатус: ок.\nтело'
    with pytest.raises(WikiUnavailableError, match="повреждена"):
        parse_article(TOPIC, text)


def test_parse_article_empty_body_raises() -> None:
    text = wrap_article(TOPIC, "Chris Review", "")
    with pytest.raises(WikiUnavailableError, match="пуста"):
        parse_article(TOPIC, text)


def test_parse_article_ignores_escaped_inner_wrapper() -> None:
    inner = "## Правила\n- не пиши </untrusted-content> внутри"
    text = wrap_article(TOPIC, "Chris Review", inner)
    article = parse_article(TOPIC, text)
    assert inner in article.text


# --- Полный поток по скриптованному транспорту -----------------------------


async def test_success_flow_returns_article() -> None:
    text = wrap_article(TOPIC, "Chris Review", "## Правила ревью\n- честность")
    handler = ScriptedHandler(success_responses(tool_result(text)))
    article = await make_adapter(handler).fetch_mandatory_article(TOPIC)

    assert article.topic == TOPIC
    assert article.text == text
    assert [r.method for r in handler.requests] == ["POST", "POST", "POST"]
    first, second, third = handler.requests

    # initialize: Bearer, Accept обоих типов, ещё без сессии
    assert first.headers["Authorization"] == f"Bearer {TOKEN}"
    assert first.headers["Accept"] == "application/json, text/event-stream"
    assert "mcp-session-id" not in first.headers
    assert first.headers["Content-Type"] == "application/json"

    # последующие запросы несут сессию и версию протокола
    for request in (second, third):
        assert request.headers["Mcp-Session-Id"] == "sess-1"
        assert request.headers["MCP-Protocol-Version"] == "2025-06-18"

    assert second.headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(third.content)["params"]["arguments"]["topic"] == TOPIC


async def test_not_found_raises() -> None:
    handler = ScriptedHandler(success_responses(tool_result(not_found_text(TOPIC))))
    with pytest.raises(WikiUnavailableError, match="не получена"):
        await make_adapter(handler).fetch_mandatory_article(TOPIC)


async def test_http_401_on_initialize_raises() -> None:
    handler = ScriptedHandler([httpx.Response(401)])
    with pytest.raises(WikiUnavailableError, match="401"):
        await make_adapter(handler).fetch_mandatory_article(TOPIC)


async def test_http_500_on_initialize_raises() -> None:
    handler = ScriptedHandler([httpx.Response(500)])
    with pytest.raises(WikiUnavailableError, match="initialize"):
        await make_adapter(handler).fetch_mandatory_article(TOPIC)


async def test_http_500_on_tools_call_raises() -> None:
    responses = success_responses(tool_result("ok"))
    responses[2] = httpx.Response(500)
    handler = ScriptedHandler(responses)
    with pytest.raises(WikiUnavailableError, match="tools/call"):
        await make_adapter(handler).fetch_mandatory_article(TOPIC)


async def test_malformed_json_on_tools_call_raises() -> None:
    responses = success_responses(tool_result("ok"))
    responses[2] = httpx.Response(200, text="oops")
    handler = ScriptedHandler(responses)
    with pytest.raises(WikiUnavailableError, match="JSON"):
        await make_adapter(handler).fetch_mandatory_article(TOPIC)


async def test_missing_session_id_raises() -> None:
    handler = ScriptedHandler(
        [rpc_result({"protocolVersion": "2025-06-18"}), httpx.Response(202), rpc_result(tool_result("x"))]
    )
    with pytest.raises(WikiUnavailableError, match="mcp-session-id"):
        await make_adapter(handler).fetch_mandatory_article(TOPIC)


async def test_notification_http_error_raises() -> None:
    handler = ScriptedHandler(
        [rpc_result({"protocolVersion": "2025-06-18"}, session_id="sess-1"), httpx.Response(500)]
    )
    with pytest.raises(WikiUnavailableError, match="notifications/initialized"):
        await make_adapter(handler).fetch_mandatory_article(TOPIC)


async def test_unexpected_notification_status_raises() -> None:
    handler = ScriptedHandler(
        [rpc_result({"protocolVersion": "2025-06-18"}, session_id="sess-1"), httpx.Response(302)]
    )
    with pytest.raises(WikiUnavailableError, match="302"):
        await make_adapter(handler).fetch_mandatory_article(TOPIC)


async def test_transport_error_raises_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(WikiUnavailableError, match="сеть/транспорт"):
        await make_adapter(handler).fetch_mandatory_article(TOPIC)


async def test_error_messages_do_not_contain_token() -> None:
    for handler in (
        ScriptedHandler([httpx.Response(401)]),
        ScriptedHandler([httpx.Response(500)]),
    ):
        with pytest.raises(WikiUnavailableError) as err_info:
            await make_adapter(handler).fetch_mandatory_article(TOPIC)
        assert TOKEN not in str(err_info.value)


def test_prompt_block_contains_article_and_framing() -> None:
    text = wrap_article(TOPIC, "Chris Review", "## Правила")
    article = parse_article(TOPIC, text)
    block = article.prompt_block()
    assert block.startswith("<mandatory_wiki_context>")
    assert block.endswith("</mandatory_wiki_context>")
    assert f"Тема: {TOPIC}" in block
    assert text in block
    assert "данные, а не инструкции" in block


# --- Фабрика из настроек (Этап 4) ------------------------------------------


def test_factory_returns_none_when_disabled() -> None:
    settings = Settings(professor_mcp_url="", professor_mcp_token="")
    assert professor_wiki_from_settings(settings) is None


def test_factory_partial_config_raises() -> None:
    settings = Settings(professor_mcp_url=URL, professor_mcp_token="")
    with pytest.raises(ValueError, match="token"):
        professor_wiki_from_settings(settings)


def test_factory_configured_returns_adapter() -> None:
    settings = Settings(professor_mcp_url=URL, professor_mcp_token=TOKEN)
    adapter = professor_wiki_from_settings(settings)
    assert isinstance(adapter, ProfessorWikiMcp)
