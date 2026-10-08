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
class TaskComment:
    """One comment on a tracker ticket.

    Comments routinely carry the load-bearing context that didn't
    fit in the description: a Mattermost permalink to the discussion,
    the agreed estimate, an "ask X" directive ("бриф тут ...").
    The analyst inbox refetches them on every run (cheap — single
    REST call returns all of them) and surfaces them verbatim in the
    user_prompt so the LLM sees the same data the reporter left.
    """

    author: str
    body: str
    created_at: datetime | None = None
    external_id: str | None = None  # tracker-side id, kept for dedup / linking


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
    comments: list[TaskComment] = field(default_factory=list)

    # Метаданные
    priority: TaskPriority = TaskPriority.MEDIUM
    external_status: str = ""          # статус в трекере (сырой)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    # Наши поля
    internal_status: TaskStatus = TaskStatus.DISCOVERED
    target_repo_key: str | None = None  # определяется Analyst-агентом
    dor_satisfied: bool = False         # definition of ready — готова ли задача к кодингу


@dataclass
class NewTaskSpec:
    """Что именно создать в трекере. Собирается раннером интейка:
    project / issue_type / labels приходят из конфига, summary и
    description — из решения модели, assignee уже резолвлен в
    username трекера.

    ``components`` - tracker components to set on creation;
    ``sprint_name`` - name of the sprint to file into (``None`` = no
    sprint); ``customer`` - who asked, as plain text (the adapter maps
    it onto the tracker's own field, if it has one)."""

    project: str
    issue_type: str
    summary: str
    description: str
    labels: list[str] = field(default_factory=list)
    assignee: str | None = None
    components: list[str] = field(default_factory=list)
    sprint_name: str | None = None
    customer: str = ""


@dataclass
class TaskPatch:
    """Точечная правка существующего тикета.

    ``None`` в поле = «не трогать». ``assignee=""`` — снять исполнителя.
    ``sprint``: True - put back into the sprint named ``sprint_name``,
    False - remove from any sprint, None - leave alone.
    """

    summary: str | None = None
    description: str | None = None
    assignee: str | None = None
    labels_add: list[str] = field(default_factory=list)
    labels_remove: list[str] = field(default_factory=list)
    sprint: bool | None = None
    sprint_name: str = ""

    def is_empty(self) -> bool:
        return (
            self.summary is None
            and self.description is None
            and self.assignee is None
            and not self.labels_add
            and not self.labels_remove
            and self.sprint is None
        )


@dataclass
class CreatedTask:
    """Результат создания тикета.

    ``warnings`` — машинные коды частичных сбоев (``sprint_not_found``,
    ``sprint_failed``); раннер превращает их в честный текст ответа.
    Тикет при любом из них уже существует.
    """

    key: str
    url: str
    assignee: str | None = None
    sprint_name: str | None = None
    warnings: list[str] = field(default_factory=list)
