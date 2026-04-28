"""Доменная модель задачи (тикета).

Задача трекера-агностична: не привязана к полям Jira.
Адаптер task_tracker мапит поля конкретного трекера в эту модель.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class TaskStatus(str, Enum):
    """Внутренний статус задачи в нашей системе (не статус в Jira)."""

    DISCOVERED = "discovered"   # только что забрали из трекера
    PLANNING = "planning"        # Analyst строит план
    CLARIFYING = "clarifying"    # Communicator собирает уточнения
    READY = "ready"              # план готов, уточнения собраны
    CODING = "coding"            # Dev-агент пишет код
    MR_OPEN = "mr_open"          # MR открыт, ждёт ревью
    REVIEWING = "reviewing"      # идёт цикл ревью
    MERGED = "merged"            # смержен
    DONE = "done"                # тикет закрыт в трекере
    FAILED = "failed"            # что-то пошло не так
    ESCALATED = "escalated"      # эскалирован человеку


class TaskPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class TaskLink:
    """Внешняя ссылка из описания задачи (на Confluence, Mattermost-тред, etc).

    ``kind`` values currently in use:
    * ``jira_attachment`` — file attached to the ticket; ``name`` and
      ``external_id`` are populated.
    * ``jira_issue`` — another Jira ticket linked via ``issuelinks``.
      ``external_id`` is the linked key (e.g. ``DM-3215``);
      ``relationship`` is the link type from the linker's POV ("is
      linked with", "blocks", "is blocked by", "duplicates", ...);
      ``summary`` / ``status`` are the linked ticket's title and
      Jira-status, taken from the inline issuelink payload.
    * ``remote_link`` — ``object.url`` of a Jira remote link; used for
      Confluence "mentioned in" back-references and similar. ``url``
      points off-Jira (Confluence page etc.); ``relationship`` carries
      the Jira label ("mentioned in", "Wiki Page", ...); ``summary``
      is the remote-link title (often a generic "Page" — Jira doesn't
      preserve the real Confluence page title here, fetch the URL to
      get the real content).
    """

    url: str
    kind: str
    # Optional metadata. Different kinds use different subsets — each
    # field is documented above.
    name: str | None = None
    external_id: str | None = None
    relationship: str | None = None
    summary: str | None = None
    status: str | None = None


@dataclass
class Task:
    """Задача (тикет), которую бот должен выполнить.

    Абстракция над Jira Issue / Trello Card / GitHub Issue.
    """

    # Идентификация
    external_id: str                  # например "DM-1234"
    tracker: str                      # "jira" | "trello" | ...
    title: str
    description: str

    # Связи
    url: str                          # прямая ссылка на тикет
    assignee_id: str | None = None
    reporter_id: str | None = None
    components: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    links: list[TaskLink] = field(default_factory=list)

    # Метаданные
    priority: TaskPriority = TaskPriority.MEDIUM
    external_status: str = ""          # статус в трекере (сырой)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    # Наши поля
    internal_status: TaskStatus = TaskStatus.DISCOVERED
    target_repo_key: str | None = None  # определяется Analyst-агентом
    dor_satisfied: bool = False         # definition of ready — готова ли задача к кодингу
