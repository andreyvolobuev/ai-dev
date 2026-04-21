"""Доменные модели для чата (Mattermost / Slack / ...)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class ChatUser:
    """Пользователь чата."""

    id: str
    username: str
    email: str | None = None
    display_name: str | None = None
    is_bot: bool = False


@dataclass
class ChatChannel:
    """Канал в чате."""

    id: str
    name: str
    is_direct: bool = False          # личка
    is_private: bool = False


@dataclass
class ChatMessage:
    """Сообщение в чате. Trusted=True только если автор — бот сам или наш orchestrator.

    ВАЖНО: всё, что пришло от людей, имеет trusted=False и должно пропускаться через
    injection-фильтр перед подачей в LLM-контекст.
    """

    id: str
    channel_id: str
    author_id: str
    text: str
    timestamp: datetime
    thread_root_id: str | None = None  # id корневого сообщения треда, если это ответ
    trusted: bool = False              # True только для наших собственных сообщений
