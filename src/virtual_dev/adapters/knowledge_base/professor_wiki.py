"""MCP-адаптер обязательной статьи Профессора (streamable HTTP + Bearer).

Реализует минимальный клиент MCP поверх httpx: initialize →
notifications/initialized → tools/call. Профессор работает в stateful-режиме
(``json_response=True, stateless=False``): идентификатор сессии приходит в
заголовке ответа ``initialize`` и передаётся дальше в ``Mcp-Session-Id``.

Важно: флаг ``found`` из executor-инструмента НЕ доезжает до провода —
не найденная статья возвращается с ``isError: false``. Успех определяется
по форме ответа (untrusted-обёртка с запрошенной темой), а не по JSON-полю.
"""

from typing import Any

import httpx

from virtual_dev.domain.models.wiki_article import WikiArticle
from virtual_dev.domain.ports.professor_wiki import ProfessorWikiPort, WikiUnavailableError
from virtual_dev.infrastructure.config import Settings

_PROTOCOL_VERSION = "2025-06-18"
_ACCEPT = "application/json, text/event-stream"
_UNTRUSTED_CLOSE = "</untrusted_content>"
_TITLE_LINES = 2  # строка 1 — заголовок статьи, строка 2 — статус


def build_initialize_payload(protocol_version: str = _PROTOCOL_VERSION) -> dict[str, Any]:
    """JSON-RPC payload инициализации MCP-сессии."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": protocol_version,
            "capabilities": {},
            "clientInfo": {"name": "virtual-dev", "version": "1.0"},
        },
    }


def build_initialized_notification() -> dict[str, Any]:
    """Уведомление ``notifications/initialized`` — без поля ``id`` (не запрос)."""
    return {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}


def build_tools_call_payload(call_id: int, topic: str) -> dict[str, Any]:
    """JSON-RPC payload вызова ``wiki_get_page``."""
    return {
        "jsonrpc": "2.0",
        "id": call_id,
        "method": "tools/call",
        "params": {"name": "wiki_get_page", "arguments": {"topic": topic}},
    }


def untrusted_open_tag(topic: str) -> str:
    """Точный открывающий тег обёртки Профессора для темы.

    Профессор собирает ``source`` из ``lotsiya:{topic}`` без маскирования
    слэша, поэтому точное совпадение строки служит проверкой темы.
    """
    return f'<untrusted_content source="lotsiya:{topic}">'


def parse_session_id(response: httpx.Response) -> str:
    """Извлечь идентификатор сессии из ответа ``initialize``."""
    session_id: str | None = response.headers.get("mcp-session-id")
    if not session_id:
        raise WikiUnavailableError("Профессор: initialize не вернул mcp-session-id")
    return session_id


def parse_initialize_protocol(response: httpx.Response, requested: str) -> str:
    """Вернуть эхо ``protocolVersion`` сервера (фолбэк — запрошенная версия)."""
    try:
        body = response.json()
    except ValueError as err:
        raise WikiUnavailableError("Профессор: некорректный JSON в initialize") from err
    result = body.get("result") if isinstance(body, dict) else None
    echoed = result.get("protocolVersion") if isinstance(result, dict) else None
    return echoed if isinstance(echoed, str) else requested


def extract_tool_text(response: httpx.Response) -> str:
    """Извлечь текст из ответа ``tools/call`` с проверкой JSON-RPC и ``isError``."""
    try:
        body = response.json()
    except ValueError as err:
        raise WikiUnavailableError("Профессор: некорректный JSON в tools/call") from err
    if not isinstance(body, dict):
        raise WikiUnavailableError("Профессор: неожиданный ответ tools/call (не объект)")
    if "error" in body:
        raise WikiUnavailableError(f"Профессор: JSON-RPC ошибка tools/call: {body['error']}")
    result = body.get("result")
    if not isinstance(result, dict):
        raise WikiUnavailableError("Профессор: в tools/call нет result")
    if result.get("isError"):
        raise WikiUnavailableError("Профессор: инструмент вернул ошибку (isError=true)")
    content = result.get("content")
    if not isinstance(content, list):
        raise WikiUnavailableError("Профессор: некорректный content в tools/call")
    text = "\n".join(
        item["text"]
        for item in content
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
    )
    if not text.strip():
        raise WikiUnavailableError("Профессор: пустой текст статьи")
    return text


def parse_article(topic: str, text: str) -> WikiArticle:
    """Проверить форму успешного ответа ``wiki_get_page``.

    Успех = точная untrusted-обёртка с запрошенной темой + непустое тело
    статьи (первые две строки внутри — заголовок и статус). Всё остальное —
    включая «не найдена» с ``isError=false`` — инфраструктурная ошибка.
    """
    open_tag = untrusted_open_tag(topic)
    if open_tag not in text:
        raise WikiUnavailableError(
            f"Профессор: статья «{topic}» не получена (ответ без untrusted-обёртки)"
        )
    inner = text.split(open_tag, 1)[1]
    if _UNTRUSTED_CLOSE not in inner:
        raise WikiUnavailableError(
            f"Профессор: статья «{topic}» повреждена (нет закрывающей обёртки)"
        )
    inner = inner.split(_UNTRUSTED_CLOSE, 1)[0]
    body_lines = inner.strip().splitlines()[_TITLE_LINES:]
    if not "\n".join(body_lines).strip():
        raise WikiUnavailableError(f"Профессор: статья «{topic}» пуста")
    return WikiArticle(topic=topic, text=text)


class ProfessorWikiMcp(ProfessorWikiPort):
    """Клиент MCP Профессора: один полный fetch на вызов, без внутренних ретраев.

    Повторяемость обеспечивает вызывающая сторона (отсутствие ack при
    ``WikiUnavailableError``). Сообщения об ошибках не содержат токена и
    заголовков.
    """

    def __init__(
        self,
        *,
        url: str,
        token: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not url:
            raise ValueError("ProfessorWikiMcp: пустой url")
        if not token:
            raise ValueError("ProfessorWikiMcp: пустой token")
        self._url = url
        self._token = token
        self._timeout = timeout
        self._transport = transport

    async def fetch_mandatory_article(self, topic: str) -> WikiArticle:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": _ACCEPT,
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                self._initialize(await self._post(client, headers, build_initialize_payload()), headers)
                await self._notify_initialized(client, headers)
                return await self._call_tool(client, headers, topic)
        except WikiUnavailableError:
            raise
        except httpx.HTTPError as err:
            raise WikiUnavailableError(f"Профессор: сеть/транспорт недоступны: {err}") from err

    async def _post(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> httpx.Response:
        return await client.post(self._url, json=payload, headers=headers)

    def _initialize(self, response: httpx.Response, headers: dict[str, str]) -> None:
        self._check_status(response, "initialize")
        headers["Mcp-Session-Id"] = parse_session_id(response)
        headers["MCP-Protocol-Version"] = parse_initialize_protocol(response, _PROTOCOL_VERSION)

    async def _notify_initialized(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
    ) -> None:
        response = await self._post(client, headers, build_initialized_notification())
        if response.status_code not in (200, 202):
            raise WikiUnavailableError(
                f"Профессор: notifications/initialized вернул HTTP {response.status_code}"
            )

    async def _call_tool(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        topic: str,
    ) -> WikiArticle:
        response = await self._post(client, headers, build_tools_call_payload(2, topic))
        self._check_status(response, "tools/call")
        return parse_article(topic, extract_tool_text(response))

    @staticmethod
    def _check_status(response: httpx.Response, stage: str) -> None:
        if response.status_code == 401:
            raise WikiUnavailableError("Профессор: отклонён токен (HTTP 401)")
        if response.status_code != 200:
            raise WikiUnavailableError(f"Профессор: {stage} вернул HTTP {response.status_code}")


def professor_wiki_from_settings(settings: Settings) -> ProfessorWikiMcp | None:
    """Собрать адаптер из настроек: ``None`` — интеграция выключена.

    Частичная конфигурация — fail-fast на старте (тот же контракт, что и у
    ``tools._professor_mcp.professor_mcp_server``): ctor бросит ValueError.
    """
    if not settings.professor_mcp_url and not settings.professor_mcp_token:
        return None
    return ProfessorWikiMcp(
        url=settings.professor_mcp_url,
        token=settings.professor_mcp_token,
    )
