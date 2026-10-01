"""TaskIntakeInbox: что реально происходит по решению агента.

Главное, что здесь пинится: тикет создаётся один раз на пост,
частичные сбои (спринт, исполнитель) не отменяют тикет — правки
попадают в последний тикет треда.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents.task_intake import (
    IntakeAction,
    IntakeDecision,
    IntakeTicketState,
)
from virtual_dev.application.services import CommunicatorService, InjectionFilter
from virtual_dev.domain.models.chat import ChatMessage, ChatUser
from virtual_dev.domain.models.task import (
    CreatedTask,
    NewTaskSpec,
    Task,
    TaskPatch,
)
from virtual_dev.domain.ports.chat import ChatPort
from virtual_dev.domain.ports.task_tracker import TaskTrackerPort
from virtual_dev.infrastructure.config.schema import (
    AgentsCfg,
    AppConfig,
    MappingsCfg,
    MmTemplatesCfg,
    NotificationsCfg,
    TaskIntakeCfg,
)
from virtual_dev.infrastructure.db import IntakeRequestRow
from virtual_dev.runtime.workers.intake_inbox import (
    TaskIntakeInbox,
    _public_cause,
)

# ---------------------------- fakes ----------------------------


class _FakeChat(ChatPort):
    def __init__(self, *, users: dict[str, ChatUser] | None = None,
                 search_hits: list[ChatUser] | None = None) -> None:
        self._users = users or {}
        self._search_hits = search_hits or []
        self.sent: list[tuple[str, str, str | None]] = []

    async def send_direct(self, user_id: str, text: str) -> ChatMessage:
        return _bot_msg(text)

    async def send_to_channel(
        self, channel_id: str, text: str, thread_root_id: str | None = None,
    ) -> ChatMessage:
        self.sent.append((channel_id, text, thread_root_id))
        return _bot_msg(text)

    async def read_thread(self, thread_root_id: str) -> Sequence[ChatMessage]:
        return []

    async def find_user_by_email(self, email: str) -> ChatUser | None:
        return None

    async def find_user_by_username(self, username: str) -> ChatUser | None:
        return None

    async def search_users_by_name(
        self, query: str, *, limit: int = 25,
    ) -> Sequence[ChatUser]:
        return self._search_hits

    async def get_user_by_id(self, user_id: str) -> ChatUser | None:
        return self._users.get(user_id)

    async def post_permalink(self, post_id: str, channel_id: str) -> str | None:
        return f"https://mm.example/dm/pl/{post_id}"

    async def add_reaction(self, post_id: str, emoji_name: str) -> None:
        return None

    async def get_post(self, post_id: str) -> ChatMessage | None:
        return None

    def subscribe(self) -> AsyncIterator[ChatMessage]:
        async def _empty() -> AsyncIterator[ChatMessage]:
            if False:
                yield _bot_msg("")
        return _empty()


class _FakeTracker(TaskTrackerPort):
    def __init__(
        self,
        *,
        username_by_email: dict[str, str] | None = None,
        created: CreatedTask | None = None,
        create_raises: Exception | None = None,
        find_by_email_raises: Exception | None = None,
    ) -> None:
        self._username_by_email = username_by_email or {}
        self._created = created
        self._create_raises = create_raises
        self._find_by_email_raises = find_by_email_raises
        self.specs: list[NewTaskSpec] = []
        self.patches: list[tuple[str, TaskPatch]] = []

    async def fetch_tasks(self, jql: str, limit: int = 50) -> Sequence[Task]:
        return []

    async def get_task(self, external_id: str) -> Task:
        return Task(
            external_id=external_id, tracker="jira", title="Жёлтые карточки",
            description="", url=f"https://jira.example/browse/{external_id}",
            assignee_id="ivan.ivanov", labels=["dmp-sup"],
        )

    async def transition(self, external_id: str, to_status: str) -> None:
        return None

    async def comment(self, external_id: str, body: str) -> None:
        return None

    async def create_task(self, spec: NewTaskSpec) -> CreatedTask:
        if self._create_raises is not None:
            raise self._create_raises
        self.specs.append(spec)
        return self._created or CreatedTask(
            key="DM-4821", url="https://jira.example/browse/DM-4821",
            assignee=spec.assignee, sprint_name="Sprint 42",
        )

    async def update_task(self, external_id: str, patch: TaskPatch) -> None:
        self.patches.append((external_id, patch))

    async def find_tracker_user_by_email(self, email: str) -> str | None:
        if self._find_by_email_raises is not None:
            raise self._find_by_email_raises
        return self._username_by_email.get(email)


class _FakeAgent:
    def __init__(self, decisions: list[IntakeDecision]) -> None:
        self._decisions = decisions
        self.calls: list[IntakeTicketState | None] = []

    async def decide(
        self,
        *,
        post: ChatMessage,
        thread: Sequence[ChatMessage],
        permalink: str = "",
        existing: IntakeTicketState | None = None,
    ) -> IntakeDecision:
        self.calls.append(existing)
        return self._decisions.pop(0)


# ---------------------------- helpers ----------------------------


def _bot_msg(text: str) -> ChatMessage:
    return ChatMessage(
        id="bot-post", channel_id="chan-1", author_id="bot", text=text,
        timestamp=datetime.now(UTC), trusted=True,
    )


def _ask(text: str = "@aida заведи задачу", *, post_id: str = "p-1",
         root: str | None = None) -> ChatMessage:
    return ChatMessage(
        id=post_id, channel_id="chan-1", author_id="u1", text=text,
        timestamp=datetime.now(UTC), thread_root_id=root, trusted=False,
    )


def _cfg(*, enabled: bool = True) -> AppConfig:
    templates = MmTemplatesCfg(
        intake_created=(
            "Завела [{key}]({url}) — «{summary}». "
            "Исполнитель: {assignee}, спринт: {sprint}.{warnings_block}"
        ),
        intake_updated="Готово: {changes}",
        intake_failed="Завести задачу в Jira не вышло: {reason}.",
        intake_busy_fallback="Сейчас занята, отвлечься не могу.",
        intake_warning_no_active_sprint="активного спринта не нашла",
        intake_warning_sprint_failed="в спринт положить не получилось",
        intake_warning_assignee_not_found="не нашла тебя в Jira по почте",
        intake_warning_assignee_hint_unresolved="не поняла, кого назначить",
        intake_warning_assignee_hint_unresolved_update=(
            "не поняла, кого назначить — исполнителя не тронула"
        ),
    )
    return AppConfig(
        repositories=[],
        agents=AgentsCfg(task_intake=TaskIntakeCfg(
            enabled=enabled, project="DM", issue_type="Task",
            labels=["dmp-sup"], add_to_active_sprint=True,
        )),
        mappings=MappingsCfg(),
        notifications=NotificationsCfg(mattermost=templates),
    )


def _inbox(
    *,
    agent: _FakeAgent,
    tracker: _FakeTracker | None,
    chat: _FakeChat,
    session_factory: async_sessionmaker[AsyncSession],
    config: AppConfig | None = None,
) -> TaskIntakeInbox:
    return TaskIntakeInbox(
        agent=agent,  # type: ignore[arg-type]
        task_tracker=tracker,
        chat=chat,
        communicator=CommunicatorService(
            chat, InjectionFilter(), respect_working_hours=False,
        ),
        session_factory=session_factory,
        config=config or _cfg(),
    )


def _create_decision(**over: Any) -> IntakeDecision:
    base: dict[str, Any] = {
        "action": IntakeAction.CREATE,
        "summary": "Собрать жёлтые карточки по Грузии",
        "description": "Собрать жёлтые карточки по Грузии за сентябрь.",
        "reasoning": "прямая просьба",
    }
    base.update(over)
    return IntakeDecision(**base)


def _user(username: str = "ivanov", email: str = "ivan.ivanov@2gis.ru") -> ChatUser:
    return ChatUser(id="u1", username=username, email=email)


# ---------------------------- tests ----------------------------


async def test_create_puts_label_sprint_and_requester_as_assignee(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat = _FakeChat(users={"u1": _user()})
    tracker = _FakeTracker(username_by_email={"ivan.ivanov@2gis.ru": "ivan.ivanov"})
    agent = _FakeAgent([_create_decision()])

    outcome = await _inbox(
        agent=agent, tracker=tracker, chat=chat, session_factory=session_factory,
    ).handle(_ask())

    assert outcome.action == "created"
    assert outcome.issue_key == "DM-4821"
    spec = tracker.specs[0]
    assert spec.labels == ["dmp-sup"]
    assert spec.add_to_active_sprint is True
    assert spec.assignee == "ivan.ivanov"
    assert spec.project == "DM"
    # Ссылка на тред-источник обязана быть в описании: без неё через месяц
    # непонятно, откуда задача взялась.
    assert "https://mm.example/dm/pl/p-1" in spec.description
    # Ответ в тред несёт настоящий ключ и ссылку.
    channel, text, root = chat.sent[0]
    assert channel == "chan-1"
    assert root == "p-1"          # пост без треда сам становится корнем
    assert "DM-4821" in text
    assert "Sprint 42" in text


async def test_created_ticket_is_recorded_for_later_edits(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat = _FakeChat(users={"u1": _user()})
    tracker = _FakeTracker(username_by_email={"ivan.ivanov@2gis.ru": "ivan.ivanov"})
    await _inbox(
        agent=_FakeAgent([_create_decision()]), tracker=tracker, chat=chat,
        session_factory=session_factory,
    ).handle(_ask())

    async with session_factory() as session:
        rows = list((await session.execute(select(IntakeRequestRow))).scalars())
    assert len(rows) == 1
    assert rows[0].issue_key == "DM-4821"
    assert rows[0].mm_root_id == "p-1"
    assert rows[0].source_post_id == "p-1"


async def test_same_post_twice_creates_one_ticket(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """WS-событие и catch-up sweep приносят один и тот же пост."""
    chat = _FakeChat(users={"u1": _user()})
    tracker = _FakeTracker(username_by_email={"ivan.ivanov@2gis.ru": "ivan.ivanov"})
    inbox = _inbox(
        agent=_FakeAgent([_create_decision(), _create_decision()]),
        tracker=tracker, chat=chat, session_factory=session_factory,
    )

    first = await inbox.handle(_ask())
    second = await inbox.handle(_ask())

    assert first.action == "created"
    assert second.action == "skipped"
    assert second.reason == "duplicate_post"
    assert len(tracker.specs) == 1


async def test_unknown_jira_user_still_creates_and_says_so(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat = _FakeChat(users={"u1": _user(email="someone.else@2gis.ru")})
    tracker = _FakeTracker(username_by_email={})
    outcome = await _inbox(
        agent=_FakeAgent([_create_decision()]), tracker=tracker, chat=chat,
        session_factory=session_factory,
    ).handle(_ask())

    assert outcome.action == "created"
    assert tracker.specs[0].assignee is None
    assert "не нашла тебя в Jira по почте" in chat.sent[0][1]


async def test_named_assignee_is_resolved_via_chat_search(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat = _FakeChat(
        users={"u1": _user()},
        search_hits=[ChatUser(id="u2", username="petrov", email="petr.petrov@2gis.ru")],
    )
    tracker = _FakeTracker(username_by_email={"petr.petrov@2gis.ru": "petr.petrov"})
    await _inbox(
        agent=_FakeAgent([_create_decision(assignee_hint="Пётр Петров")]),
        tracker=tracker, chat=chat, session_factory=session_factory,
    ).handle(_ask())

    assert tracker.specs[0].assignee == "petr.petrov"


async def test_ambiguous_named_assignee_falls_back_to_requester(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Двое похожих — назначать наугад нельзя, но и задачу терять нельзя."""
    chat = _FakeChat(
        users={"u1": _user()},
        search_hits=[
            ChatUser(id="u2", username="petrov", email="petr.petrov@2gis.ru"),
            ChatUser(id="u3", username="petrovsky", email="petr.petrovsky@2gis.ru"),
        ],
    )
    tracker = _FakeTracker(username_by_email={
        "ivan.ivanov@2gis.ru": "ivan.ivanov",
        "petr.petrov@2gis.ru": "petr.petrov",
    })
    await _inbox(
        agent=_FakeAgent([_create_decision(assignee_hint="Петя")]),
        tracker=tracker, chat=chat, session_factory=session_factory,
    ).handle(_ask())

    assert tracker.specs[0].assignee == "ivan.ivanov"
    assert "не поняла, кого назначить" in chat.sent[0][1]


async def test_sprint_warning_from_tracker_reaches_the_reply(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat = _FakeChat(users={"u1": _user()})
    tracker = _FakeTracker(
        username_by_email={"ivan.ivanov@2gis.ru": "ivan.ivanov"},
        created=CreatedTask(
            key="DM-4822", url="https://jira.example/browse/DM-4822",
            assignee="ivan.ivanov", sprint_name=None,
            warnings=["no_active_sprint"],
        ),
    )
    await _inbox(
        agent=_FakeAgent([_create_decision()]), tracker=tracker, chat=chat,
        session_factory=session_factory,
    ).handle(_ask())

    assert "активного спринта не нашла" in chat.sent[0][1]


async def test_jira_failure_reports_and_releases_the_claim(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Jira упала — человек должен узнать; повтор просьбы должен
    работать (claim не должен залипнуть)."""
    chat = _FakeChat(users={"u1": _user()})
    tracker = _FakeTracker(create_raises=RuntimeError("Jira 500"))
    outcome = await _inbox(
        agent=_FakeAgent([_create_decision()]), tracker=tracker, chat=chat,
        session_factory=session_factory,
    ).handle(_ask())

    assert outcome.action == "failed"
    assert "не вышло" in chat.sent[0][1]
    async with session_factory() as session:
        rows = list((await session.execute(select(IntakeRequestRow))).scalars())
    assert rows == []


async def test_busy_replies_with_model_text_and_touches_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat = _FakeChat(users={"u1": _user()})
    tracker = _FakeTracker()
    outcome = await _inbox(
        agent=_FakeAgent([IntakeDecision(
            action=IntakeAction.BUSY,
            reply_text="Сейчас занята задачей, отвлечься не могу.",
            reasoning="не про тикет",
        )]),
        tracker=tracker, chat=chat, session_factory=session_factory,
    ).handle(_ask("@aida что думаешь про парсер?"))

    assert outcome.action == "busy"
    assert chat.sent[0][1] == "Сейчас занята задачей, отвлечься не могу."
    assert tracker.specs == []


async def test_update_patches_the_thread_ticket(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat = _FakeChat(
        users={"u1": _user()},
        search_hits=[ChatUser(id="u2", username="petrov", email="petr.petrov@2gis.ru")],
    )
    tracker = _FakeTracker(username_by_email={
        "ivan.ivanov@2gis.ru": "ivan.ivanov",
        "petr.petrov@2gis.ru": "petr.petrov",
    })
    agent = _FakeAgent([
        _create_decision(),
        IntakeDecision(
            action=IntakeAction.UPDATE,
            changes={"assignee_hint": "Пётр Петров"},
            reasoning="просят переназначить",
        ),
    ])
    inbox = _inbox(
        agent=agent, tracker=tracker, chat=chat, session_factory=session_factory,
    )

    await inbox.handle(_ask(post_id="p-1"))
    outcome = await inbox.handle(
        _ask("@aida исполнителем поставь Петю", post_id="p-2", root="p-1"),
    )

    assert outcome.action == "updated"
    key, patch = tracker.patches[0]
    assert key == "DM-4821"
    assert patch.assignee == "petr.petrov"
    # Агент во второй раз обязан был увидеть состояние тикета.
    assert agent.calls[1] is not None
    assert agent.calls[1].key == "DM-4821"


async def test_update_without_a_ticket_says_so(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat = _FakeChat(users={"u1": _user()})
    tracker = _FakeTracker()
    outcome = await _inbox(
        agent=_FakeAgent([IntakeDecision(
            action=IntakeAction.UPDATE,
            changes={"summary": "Новое имя"},
            reasoning="просят переименовать",
        )]),
        tracker=tracker, chat=chat, session_factory=session_factory,
    ).handle(_ask("@aida переименуй задачу", post_id="p-9", root="root-9"))

    assert outcome.action == "failed"
    assert tracker.patches == []
    assert chat.sent


async def test_disabled_intake_does_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat = _FakeChat(users={"u1": _user()})
    tracker = _FakeTracker()
    agent = _FakeAgent([])
    outcome = await _inbox(
        agent=agent, tracker=tracker, chat=chat,
        session_factory=session_factory, config=_cfg(enabled=False),
    ).handle(_ask())

    assert outcome.action == "skipped"
    assert outcome.reason == "disabled"
    assert agent.calls == []
    assert chat.sent == []


async def test_no_tracker_short_circuits(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Task 7 branches on exactly this reason to decide what to do next."""
    chat = _FakeChat(users={"u1": _user()})
    agent = _FakeAgent([])
    outcome = await _inbox(
        agent=agent, tracker=None, chat=chat, session_factory=session_factory,
    ).handle(_ask())

    assert outcome.action == "skipped"
    assert outcome.reason == "no_tracker"
    assert agent.calls == []
    assert chat.sent == []


async def test_update_with_only_ambiguous_assignee_hint_reports_the_real_reason(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«переназначь на Петю» при двух тёзках: патч пуст, но причина отказа
    должна называть настоящую проблему — не общее «не поняла, что
    поправить»."""
    chat = _FakeChat(
        users={"u1": _user()},
        search_hits=[
            ChatUser(id="u2", username="petrov", email="petr.petrov@2gis.ru"),
            ChatUser(id="u3", username="petrovsky", email="petr.petrovsky@2gis.ru"),
        ],
    )
    tracker = _FakeTracker(username_by_email={"petr.petrov@2gis.ru": "petr.petrov"})
    agent = _FakeAgent([
        _create_decision(),
        IntakeDecision(
            action=IntakeAction.UPDATE,
            changes={"assignee_hint": "Петя"},
            reasoning="просят переназначить",
        ),
    ])
    inbox = _inbox(
        agent=agent, tracker=tracker, chat=chat, session_factory=session_factory,
    )

    await inbox.handle(_ask(post_id="p-1"))
    outcome = await inbox.handle(
        _ask("@aida переназначь на Петю", post_id="p-2", root="p-1"),
    )

    assert outcome.action == "failed"
    assert outcome.reason == "empty_patch"
    assert tracker.patches == []
    assert "исполнителя не тронула" in chat.sent[-1][1]
    assert "что именно поправить" not in chat.sent[-1][1]


async def test_update_with_rename_and_ambiguous_assignee_warns_honestly(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«переименуй, исполнителем поставь Петю» при двух тёзках:
    переименование применяется; предупреждение не должно врать, что
    исполнителя назначили автору просьбы (как на создании) — исполнителя
    вообще не тронули."""
    chat = _FakeChat(
        users={"u1": _user()},
        search_hits=[
            ChatUser(id="u2", username="petrov", email="petr.petrov@2gis.ru"),
            ChatUser(id="u3", username="petrovsky", email="petr.petrovsky@2gis.ru"),
        ],
    )
    tracker = _FakeTracker(username_by_email={"petr.petrov@2gis.ru": "petr.petrov"})
    agent = _FakeAgent([
        _create_decision(),
        IntakeDecision(
            action=IntakeAction.UPDATE,
            changes={"summary": "Жёлтые карточки", "assignee_hint": "Петя"},
            reasoning="просят переименовать и переназначить",
        ),
    ])
    inbox = _inbox(
        agent=agent, tracker=tracker, chat=chat, session_factory=session_factory,
    )

    await inbox.handle(_ask(post_id="p-1"))
    outcome = await inbox.handle(
        _ask("@aida переименуй и переназначь на Петю", post_id="p-2", root="p-1"),
    )

    assert outcome.action == "updated"
    _key, patch = tracker.patches[0]
    assert patch.summary == "Жёлтые карточки"
    assert patch.assignee is None
    assert "исполнителя не тронула" in chat.sent[-1][1]
    assert "поставила тебя" not in chat.sent[-1][1]


async def test_redelivered_post_is_skipped_before_the_agent_runs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Второй заход по тому же посту (WS-событие + catch-up sweep) не
    должен даже спрашивать модель — иначе ``_existing_ticket`` уже видит
    тикет первой доставки, и модель может честно ответить update/busy на
    дублирующую доставку."""
    chat = _FakeChat(users={"u1": _user()})
    tracker = _FakeTracker(username_by_email={"ivan.ivanov@2gis.ru": "ivan.ivanov"})
    agent = _FakeAgent([
        _create_decision(),
        IntakeDecision(
            action=IntakeAction.UPDATE,
            changes={"summary": "Новое имя"},
            reasoning="агент не должен был увидеть этот пост дважды",
        ),
    ])
    inbox = _inbox(
        agent=agent, tracker=tracker, chat=chat, session_factory=session_factory,
    )

    first = await inbox.handle(_ask())
    second = await inbox.handle(_ask())

    assert first.action == "created"
    assert second.action == "skipped"
    assert second.reason == "duplicate_post"
    assert len(agent.calls) == 1
    assert len(chat.sent) == 1
    assert tracker.patches == []


async def test_email_lookup_failure_still_creates_the_ticket(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Jira 5xx на резолве исполнителя по почте не должен блокировать
    создание тикета — деградируем в assignee_not_found, как при пустом
    ответе."""
    chat = _FakeChat(users={"u1": _user()})
    tracker = _FakeTracker(find_by_email_raises=RuntimeError("Jira 500"))
    outcome = await _inbox(
        agent=_FakeAgent([_create_decision()]), tracker=tracker, chat=chat,
        session_factory=session_factory,
    ).handle(_ask())

    assert outcome.action == "created"
    assert tracker.specs[0].assignee is None
    assert "не нашла тебя в Jira по почте" in chat.sent[0][1]


# --------------------- sprint: только настоящий bool ---------------------


async def _create_then_update(
    session_factory: async_sessionmaker[AsyncSession],
    changes: dict[str, Any],
    *,
    chat: _FakeChat | None = None,
) -> tuple[_FakeChat, _FakeTracker, Any]:
    chat = chat or _FakeChat(users={"u1": _user()})
    tracker = _FakeTracker(username_by_email={"ivan.ivanov@2gis.ru": "ivan.ivanov"})
    inbox = _inbox(
        agent=_FakeAgent([
            _create_decision(),
            IntakeDecision(
                action=IntakeAction.UPDATE, changes=changes, reasoning="правка",
            ),
        ]),
        tracker=tracker, chat=chat, session_factory=session_factory,
    )
    await inbox.handle(_ask(post_id="p-1"))
    outcome = await inbox.handle(
        _ask("@aida поправь", post_id="p-2", root="p-1"),
    )
    return chat, tracker, outcome


async def test_null_sprint_change_does_not_drop_the_ticket_from_the_sprint(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``"sprint": null`` — обычный заполнитель «не меняем», который
    присылает модель.
    ``bool(None)`` вычистил бы поле Sprint в Jira и бот отрапортовал бы
    «убрала из спринта», хотя никто не просил."""
    chat, tracker, outcome = await _create_then_update(
        session_factory, {"summary": "Жёлтые карточки", "sprint": None},
    )

    assert outcome.action == "updated"
    _key, patch = tracker.patches[0]
    assert patch.sprint is None
    assert "спринт" not in chat.sent[-1][1]


async def test_string_false_sprint_change_is_ignored(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``"false"`` — непустая строка: ``bool`` сделал бы из неё True и
    положил тикет в спринт."""
    _chat, tracker, outcome = await _create_then_update(
        session_factory, {"summary": "Жёлтые карточки", "sprint": "false"},
    )

    assert outcome.action == "updated"
    _key, patch = tracker.patches[0]
    assert patch.sprint is None


async def test_only_a_non_boolean_sprint_change_leaves_nothing_to_patch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat, tracker, outcome = await _create_then_update(
        session_factory, {"sprint": None},
    )

    assert outcome.action == "failed"
    assert outcome.reason == "empty_patch"
    assert tracker.patches == []
    assert "спринт" not in chat.sent[-1][1]


async def test_real_sprint_true_is_applied(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat, tracker, outcome = await _create_then_update(
        session_factory, {"sprint": True},
    )

    assert outcome.action == "updated"
    _key, patch = tracker.patches[0]
    assert patch.sprint is True
    assert "вернула в спринт" in chat.sent[-1][1]


async def test_real_sprint_false_is_applied(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat, tracker, outcome = await _create_then_update(
        session_factory, {"sprint": False},
    )

    assert outcome.action == "updated"
    _key, patch = tracker.patches[0]
    assert patch.sprint is False
    assert "убрала из спринта" in chat.sent[-1][1]


# --------------------- описание: футер не теряется ---------------------


async def test_description_patch_keeps_the_source_link_footer(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«Допиши про Армению» не должно уносить ссылку на тред-источник:
    патч в Jira заменяет описание целиком, футер дописываем мы."""
    _chat, tracker, outcome = await _create_then_update(
        session_factory,
        {"description": "Собрать жёлтые карточки по Грузии и по Армении."},
    )

    assert outcome.action == "updated"
    _key, patch = tracker.patches[0]
    assert patch.description is not None
    assert "по Армении" in patch.description
    assert "https://mm.example/dm/pl/p-1" in patch.description
    assert "Просьба от: @ivanov" in patch.description


# --------------------- отказ «занята»: кап на прозу модели -------------


async def test_empty_busy_reply_uses_the_template(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat = _FakeChat(users={"u1": _user()})
    outcome = await _inbox(
        agent=_FakeAgent([IntakeDecision(
            action=IntakeAction.BUSY, reply_text="   ", reasoning="не про тикет",
        )]),
        tracker=_FakeTracker(), chat=chat, session_factory=session_factory,
    ).handle(_ask("@aida что думаешь?"))

    assert outcome.action == "busy"
    assert chat.sent[0][1] == "Сейчас занята, отвлечься не могу."


async def test_overlong_busy_reply_falls_back_to_the_template(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Проза модели, прочитавшей чужой тред, уходит в командный канал как
    есть — простыня из инъекции не должна туда попасть."""
    chat = _FakeChat(users={"u1": _user()})
    megaphone = "Внимание всем сотрудникам: " + ("срочно смените пароли. " * 20)
    assert len(megaphone) > 300
    outcome = await _inbox(
        agent=_FakeAgent([IntakeDecision(
            action=IntakeAction.BUSY, reply_text=megaphone, reasoning="инъекция",
        )]),
        tracker=_FakeTracker(), chat=chat, session_factory=session_factory,
    ).handle(_ask("@aida что думаешь?"))

    assert outcome.action == "busy"
    assert chat.sent[0][1] == "Сейчас занята, отвлечься не могу."
    assert "смените пароли" not in chat.sent[0][1]


async def test_busy_reply_at_the_cap_still_goes_through(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat = _FakeChat(users={"u1": _user()})
    text = "Занята " + "x" * 290
    assert len(text) <= 300
    await _inbox(
        agent=_FakeAgent([IntakeDecision(
            action=IntakeAction.BUSY, reply_text=text, reasoning="не про тикет",
        )]),
        tracker=_FakeTracker(), chat=chat, session_factory=session_factory,
    ).handle(_ask("@aida что думаешь?"))

    assert chat.sent[0][1] == text


# --------------------- причина сбоя: без внутренних URL ----------------


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _http_error(status_code: int) -> Exception:
    exc = RuntimeError(
        f"{status_code} Server Error: for url: "
        "https://jira.internal.example/rest/api/2/issue?token=s3cret"
    )
    exc.response = _FakeResponse(status_code)  # type: ignore[attr-defined]
    return exc


async def test_jira_failure_reply_does_not_leak_the_internal_url(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``HTTPError`` несёт полный внутренний REST-URL, но ответ бота читают
    соседние команды."""
    chat = _FakeChat(users={"u1": _user()})
    tracker = _FakeTracker(create_raises=_http_error(500))
    outcome = await _inbox(
        agent=_FakeAgent([_create_decision()]), tracker=tracker, chat=chat,
        session_factory=session_factory,
    ).handle(_ask())

    assert outcome.action == "failed"
    reply = chat.sent[0][1]
    assert "jira.internal.example" not in reply
    assert "s3cret" not in reply
    assert "rest/api" not in reply
    assert "Jira не ответила" in reply


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_http_error(403), "нет прав"),
        (_http_error(401), "нет прав"),
        (_http_error(404), "не нашла проект"),
        (_http_error(400), "Jira не приняла поля задачи"),
        (_http_error(503), "Jira не ответила"),
        (ConnectionResetError("connection reset by peer"), "Jira не ответила"),
        (TimeoutError("timed out"), "Jira не ответила"),
        (ValueError("something odd"), "что-то сломалось на стороне Jira"),
    ],
)
def test_public_cause_maps_exceptions_to_short_human_text(
    exc: Exception, expected: str,
) -> None:
    assert _public_cause(exc) == expected
