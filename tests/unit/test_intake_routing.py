"""Routing of bot mentions in MmThreadListener.

Route order is the most fragile part of the feature: the bot's own threads
(MR review, escalation, analyst question) must beat intake, and a mention in
a channel root post must still reach intake even though the old code simply
returned on "no thread_root_id".
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents.thread_responder import (
    ResponderAction,
    ResponderDecision,
)
from virtual_dev.application.services import CommunicatorService, InjectionFilter
from virtual_dev.domain.models.chat import ChatMessage, ChatUser
from virtual_dev.domain.models.task import TaskStatus
from virtual_dev.domain.ports.chat import ChatPort
from virtual_dev.infrastructure.config import (
    AgentsCfg,
    AppConfig,
    MappingsCfg,
    Settings,
)
from virtual_dev.infrastructure.db import (
    IntakeRequestRow,
    MergeRequestRow,
    TaskRow,
)
from virtual_dev.runtime.workers.intake_inbox import IntakeOutcome
from virtual_dev.runtime.workers.mm_thread_listener import (
    _PROCESSED_REACTION,
    MmThreadListener,
)


class _Chat(ChatPort):
    def __init__(
        self,
        *,
        reactions: dict[str, list[str]] | None = None,
        catchup_posts: dict[str, list[ChatMessage]] | None = None,
    ) -> None:
        self._reactions = reactions or {}
        self._catchup_posts = catchup_posts or {}
        self.sent: list[tuple[str, str]] = []
        self.added_reactions: list[tuple[str, str]] = []
        self.posts: dict[str, ChatMessage] = {}
        self.read_channel_calls: list[tuple[str, datetime]] = []

    async def send_direct(self, user_id: str, text: str) -> ChatMessage:
        return _post("bot", text, author="bot", trusted=True)

    async def send_to_channel(
        self, channel_id: str, text: str, thread_root_id: str | None = None,
    ) -> ChatMessage:
        self.sent.append((channel_id, text))
        return _post("bot", text, author="bot", trusted=True)

    async def read_thread(self, thread_root_id: str) -> Sequence[ChatMessage]:
        return []

    async def find_user_by_email(self, email: str) -> ChatUser | None:
        return None

    async def find_user_by_username(self, username: str) -> ChatUser | None:
        return None

    async def add_reaction(self, post_id: str, emoji_name: str) -> None:
        self.added_reactions.append((post_id, emoji_name))

    async def get_post(self, post_id: str) -> ChatMessage | None:
        stored = self.posts.get(post_id)
        if stored is None:
            return None
        stored.bot_reactions = list(self._reactions.get(post_id, []))
        return stored

    async def read_channel_since(
        self, channel_id: str, since: datetime,
    ) -> list[ChatMessage]:
        self.read_channel_calls.append((channel_id, since))
        return [
            m for m in self._catchup_posts.get(channel_id, [])
            if m.timestamp > since
        ]

    def subscribe(self) -> AsyncIterator[ChatMessage]:
        async def _empty() -> AsyncIterator[ChatMessage]:
            if False:
                yield _post("x", "")
        return _empty()


class _IntakeStub:
    def __init__(self, outcome: IntakeOutcome | None = None) -> None:
        self.calls: list[ChatMessage] = []
        self._outcome = outcome or IntakeOutcome(
            action="created", issue_key="DM-4821", reply_sent=True,
        )

    async def handle(self, event: ChatMessage) -> IntakeOutcome:
        self.calls.append(event)
        return self._outcome


class _AnalystStub:
    """Only the methods the listener calls on analyst_inbox."""

    def __init__(
        self, *, by_thread: object | None = None, by_channel: object | None = None,
    ) -> None:
        self._by_thread = by_thread
        self._by_channel = by_channel
        self.fragments: list[str] = []

    async def find_task_by_thread(self, thread_root_id: str) -> object | None:
        return self._by_thread

    async def find_task_by_channel(
        self, *, mm_channel_id: str, mm_user_id: str,
    ) -> object | None:
        return self._by_channel

    async def append_fragment(self, task_id: int, event: ChatMessage) -> None:
        self.fragments.append(event.id)


class _TaskRow:
    id = 42


class _ResponderStub:
    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, **kwargs: Any) -> ResponderDecision:
        self.calls += 1
        return ResponderDecision(
            action=ResponderAction.IGNORE, reasoning="stub",
        )


def _post(
    post_id: str,
    text: str,
    *,
    author: str = "u1",
    root: str | None = None,
    trusted: bool = False,
) -> ChatMessage:
    return ChatMessage(
        id=post_id, channel_id="chan-1", author_id=author, text=text,
        timestamp=datetime.now(UTC), thread_root_id=root, trusted=trusted,
    )


def _listener(
    *,
    chat: _Chat,
    session_factory: async_sessionmaker[AsyncSession],
    intake: _IntakeStub | None,
    responder: _ResponderStub | None = None,
    analyst: _AnalystStub | None = None,
) -> MmThreadListener:
    return MmThreadListener(
        chat=chat,
        communicator=CommunicatorService(
            chat, InjectionFilter(), respect_working_hours=False,
        ),
        responder=responder or _ResponderStub(),  # type: ignore[arg-type]
        dev_agents={},
        session_factory=session_factory,
        config=AppConfig(
            repositories=[], agents=AgentsCfg(), mappings=MappingsCfg(),
        ),
        settings=Settings(mattermost_bot_username="aida"),
        analyst_inbox=analyst,  # type: ignore[arg-type]
        intake_inbox=intake,  # type: ignore[arg-type]
    )


async def test_mention_in_root_post_reaches_intake(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Сценарий 1: тегнули в канале, треда нет. Старый код тут просто
    выходил по `if not event.thread_root_id`."""
    chat = _Chat()
    event = _post("p-1", "@aida заведи задачу на жёлтые карточки")
    chat.posts["p-1"] = event
    intake = _IntakeStub()

    await _listener(
        chat=chat, session_factory=session_factory, intake=intake,
    )._dispatch(event)

    assert [e.id for e in intake.calls] == ["p-1"]
    assert (("p-1", _PROCESSED_REACTION)) in chat.added_reactions


async def test_mention_in_thread_reaches_intake(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Сценарий 2: обсуждение в треде, потом упоминание бота."""
    chat = _Chat()
    event = _post("p-2", "@aida прочитай тред и создай задачу", root="root-1")
    chat.posts["p-2"] = event
    intake = _IntakeStub()

    await _listener(
        chat=chat, session_factory=session_factory, intake=intake,
    )._dispatch(event)

    assert [e.id for e in intake.calls] == ["p-2"]


async def test_post_without_mention_is_not_intake(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Правки в интейк-треде принимаются только по повторному упоминанию —
    иначе бот вклинивался бы в живое обсуждение соседей."""
    chat = _Chat()
    event = _post("p-3", "исполнителем поставь Петю", root="root-1")
    chat.posts["p-3"] = event
    intake = _IntakeStub()

    await _listener(
        chat=chat, session_factory=session_factory, intake=intake,
    )._dispatch(event)

    assert intake.calls == []


async def test_mention_in_review_thread_goes_to_responder(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A mention in an MR review thread is a review, not a ticket request."""
    async with session_factory() as session:
        # external_id и author_username — NOT NULL без дефолта, без них
        # вставка падает на IntegrityError.
        session.add(MergeRequestRow(
            repo_key="repo-a", iid=7, external_id="7", title="MR",
            description="", author_username="aida-bot",
            web_url="https://gitlab.example/mr/7",
            source_branch="feat/x", target_branch="main",
            review_thread_root_id="root-mr", review_thread_channel_id="chan-1",
        ))
        await session.commit()

    chat = _Chat()
    event = _post("p-4", "@aida поправь тут нейминг", root="root-mr")
    chat.posts["p-4"] = event
    intake = _IntakeStub()
    responder = _ResponderStub()

    await _listener(
        chat=chat, session_factory=session_factory,
        intake=intake, responder=responder,
    )._dispatch(event)

    assert intake.calls == []
    assert responder.calls == 1


async def test_already_processed_post_is_not_reprocessed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """✅ от бота = уже обработано (catch-up sweep приносит те же посты)."""
    chat = _Chat(reactions={"p-5": [_PROCESSED_REACTION]})
    event = _post("p-5", "@aida заведи задачу")
    chat.posts["p-5"] = event
    intake = _IntakeStub()

    await _listener(
        chat=chat, session_factory=session_factory, intake=intake,
    )._dispatch(event)

    assert intake.calls == []


async def test_intake_not_wired_means_no_route(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Без инбокса (нет трекера / фича не собрана) маршрут не появляется."""
    chat = _Chat()
    event = _post("p-6", "@aida заведи задачу")
    chat.posts["p-6"] = event

    listener = _listener(chat=chat, session_factory=session_factory, intake=None)
    await listener._dispatch(event)

    assert chat.added_reactions == []


async def test_bot_own_post_is_ignored(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Бот цитирует свой же хендл в ответе — не должен звать сам себя."""
    chat = _Chat()
    event = _post("p-7", "@aida заведи задачу", author="bot", trusted=True)
    chat.posts["p-7"] = event
    intake = _IntakeStub()

    await _listener(
        chat=chat, session_factory=session_factory, intake=intake,
    )._dispatch(event)

    assert intake.calls == []


@pytest.mark.parametrize(
    "text", ["@aidanov заведи задачу", "напиши x@aida.com", "@aida.petrov глянь"],
)
async def test_near_miss_handles_are_not_mentions(
    session_factory: async_sessionmaker[AsyncSession], text: str,
) -> None:
    chat = _Chat()
    event = _post("p-8", text)
    chat.posts["p-8"] = event
    intake = _IntakeStub()

    await _listener(
        chat=chat, session_factory=session_factory, intake=intake,
    )._dispatch(event)

    assert intake.calls == []


@pytest.mark.parametrize(
    "text",
    ["@aida, заведи задачу", "Заведи задачу, @aida.", "@AIDA заведи", "please @aida: задачу"],
)
async def test_exact_mention_with_punctuation_matches(
    session_factory: async_sessionmaker[AsyncSession], text: str,
) -> None:
    chat = _Chat()
    event = _post("p-9", text)
    chat.posts["p-9"] = event
    intake = _IntakeStub()

    await _listener(
        chat=chat, session_factory=session_factory, intake=intake,
    )._dispatch(event)

    assert [e.id for e in intake.calls] == ["p-9"]


async def test_mention_in_analyst_question_thread_yields_to_analyst(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat = _Chat()
    event = _post("p-10", "@aida вот ответ", root="root-q")
    chat.posts["p-10"] = event
    intake = _IntakeStub()
    analyst = _AnalystStub(by_thread=_TaskRow())

    await _listener(
        chat=chat, session_factory=session_factory, intake=intake, analyst=analyst,
    )._dispatch(event)

    assert intake.calls == []
    assert analyst.fragments == ["p-10"]


async def test_channel_fallback_does_not_swallow_mention(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The reason intake sits above the analyst route: the channel-level
    fallback must not eat a mention outside the analyst's thread."""
    chat = _Chat()
    event = _post("p-11", "@aida заведи задачу")
    chat.posts["p-11"] = event
    intake = _IntakeStub()
    analyst = _AnalystStub(by_thread=None, by_channel=_TaskRow())

    await _listener(
        chat=chat, session_factory=session_factory, intake=intake, analyst=analyst,
    )._dispatch(event)

    assert [e.id for e in intake.calls] == ["p-11"]
    assert analyst.fragments == []


async def test_failed_intake_with_undelivered_reply_releases_claim(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat = _Chat()
    event = _post("p-12", "@aida заведи задачу")
    chat.posts["p-12"] = event
    intake = _IntakeStub(IntakeOutcome(action="failed", reply_sent=False))
    listener = _listener(chat=chat, session_factory=session_factory, intake=intake)

    await listener._dispatch(event)

    assert len(intake.calls) == 1
    assert chat.added_reactions == []
    assert not await listener._post_already_claimed("p-12")


async def test_created_with_undelivered_reply_keeps_claim_and_reaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat = _Chat()
    event = _post("p-13", "@aida заведи задачу")
    chat.posts["p-13"] = event
    intake = _IntakeStub(
        IntakeOutcome(action="created", issue_key="DM-1", reply_sent=False),
    )
    listener = _listener(chat=chat, session_factory=session_factory, intake=intake)

    await listener._dispatch(event)

    assert ("p-13", _PROCESSED_REACTION) in chat.added_reactions
    assert await listener._post_already_claimed("p-13")


async def test_busy_falls_through_to_the_analyst_that_awaits_this_post(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Задача ждёт ответа от этого человека в этом канале, он пишет
    «@aida да, только жёлтые карточки» — интейк выигрывает маршрут по
    замыслу, но ответом «занята» он не имеет права пост съесть: без
    fall-through аналитик ждёт вечно."""
    chat = _Chat()
    event = _post("p-14", "@aida да, только жёлтые карточки")
    chat.posts["p-14"] = event
    intake = _IntakeStub(IntakeOutcome(action="busy", reply_sent=True))
    analyst = _AnalystStub(by_thread=None, by_channel=_TaskRow())
    listener = _listener(
        chat=chat, session_factory=session_factory, intake=intake, analyst=analyst,
    )

    await listener._dispatch(event)

    assert [e.id for e in intake.calls] == ["p-14"]
    assert analyst.fragments == ["p-14"]
    # Без ✅: для живых маршрутов ниже пост не помечен обработанным.
    # Claim при этом остаётся: он управляет только повторной доставкой,
    # которая уже не нужна (см. тест на цикл дублей ниже).
    assert chat.added_reactions == []
    assert await listener._post_already_claimed("p-14")


async def test_busy_in_the_analyst_question_thread_also_falls_through(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Тред-вариант того же: совпадение по ``find_task_by_thread``."""
    chat = _Chat()
    event = _post("p-15", "@aida да, только жёлтые", root="root-q")
    chat.posts["p-15"] = event
    intake = _IntakeStub(IntakeOutcome(action="busy", reply_sent=True))

    class _ThreadOnlyAnalyst(_AnalystStub):
        """Совпадает по треду, по каналу — нет: интейк-маршрут вообще
        дошёл бы сюда только из канала, не из вопроса бота."""

        async def find_task_by_thread(self, thread_root_id: str) -> object | None:
            return _TaskRow() if thread_root_id == "root-q" else None

    analyst = _ThreadOnlyAnalyst(by_thread=None, by_channel=None)
    listener = _listener(
        chat=chat, session_factory=session_factory, intake=intake, analyst=analyst,
    )
    # Маршрут выше интейка (`_belongs_to_bot_thread`) здесь не срабатывает:
    # проверяем именно ветку "busy" внутри `_handle_intake`.
    assert await listener._handle_intake(event) is False
    assert analyst.fragments == []


async def test_busy_without_a_waiting_analyst_task_is_still_final(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Обычный «занята»: пост обработан, ✅ стоит, claim держится — иначе
    catch-up принёс бы пост снова и бот отказал бы второй раз."""
    chat = _Chat()
    event = _post("p-16", "@aida что думаешь про парсер?")
    chat.posts["p-16"] = event
    intake = _IntakeStub(IntakeOutcome(action="busy", reply_sent=True))
    analyst = _AnalystStub(by_thread=None, by_channel=None)
    listener = _listener(
        chat=chat, session_factory=session_factory, intake=intake, analyst=analyst,
    )

    await listener._dispatch(event)

    assert [e.id for e in intake.calls] == ["p-16"]
    assert analyst.fragments == []
    assert ("p-16", _PROCESSED_REACTION) in chat.added_reactions
    assert await listener._post_already_claimed("p-16")


async def test_busy_with_no_analyst_inbox_at_all_is_still_final(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat = _Chat()
    event = _post("p-17", "@aida что думаешь про парсер?")
    chat.posts["p-17"] = event
    intake = _IntakeStub(IntakeOutcome(action="busy", reply_sent=True))
    listener = _listener(chat=chat, session_factory=session_factory, intake=intake)

    await listener._dispatch(event)

    assert ("p-17", _PROCESSED_REACTION) in chat.added_reactions
    assert await listener._post_already_claimed("p-17")


async def test_fallthrough_post_is_not_replayed_by_the_catchup_sweep(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Защита от цикла дублей.

    Внутри канала сегодня уже заводили задачу, поэтому intake-курсор
    свипа стоит на `now - 24h` и перебивает курсор аналитика (берётся
    минимум). Пост, который ветка "busy" намеренно отдала аналитику,
    свип тянул снова на каждом тике: intake-агент крутился заново и
    публиковал ещё одно «занята», примерно каждую минуту всё
    coalescer-окно.
    """
    # Канал, где сегодня заводили задачу -> intake-курсор на 24 часа.
    async with session_factory() as session:
        session.add(IntakeRequestRow(
            source_post_id="p-earlier",
            mm_root_id="p-earlier",
            mm_channel_id="chan-1",
            requester_mm_user_id="u1",
            issue_key="DM-100",
            created_at=datetime.now(UTC) - timedelta(hours=2),
        ))
        session.add(TaskRow(
            tracker="jira", external_id="DM-101",
            title="t", description="", url="",
            priority="medium", external_status="To Do",
            internal_status=TaskStatus.PLANNING.value,
            awaiting_post_id="bot-post-q",
            awaiting_user_id="u1",
            awaiting_username="alice",
            awaiting_channel_id="chan-1",
            coalesce_window_seconds=600,
        ))
        await session.commit()

    event = _post("p-18", "@aida да, только жёлтые карточки")
    chat = _Chat(catchup_posts={"chan-1": [event]})
    chat.posts["p-18"] = event

    class _BusyIntake(_IntakeStub):
        """Настоящий инбокс на "busy" сам публикует «занята» в канал —
        дубли, которые ловит этот тест, видны именно в чате."""

        async def handle(self, incoming: ChatMessage) -> IntakeOutcome:
            await chat.send_to_channel(incoming.channel_id, "Я сейчас занята")
            return await super().handle(incoming)

    intake = _BusyIntake(IntakeOutcome(action="busy", reply_sent=True))
    analyst = _AnalystStub(by_thread=None, by_channel=_TaskRow())
    listener = _listener(
        chat=chat, session_factory=session_factory, intake=intake, analyst=analyst,
    )

    # Живая доставка: fall-through сработал, аналитик получил фрагмент (B).
    await listener._dispatch(event)
    assert analyst.fragments == ["p-18"]

    await listener.catch_up()

    # Свип канал опрашивает (пункт A не ослаблен), но пост уже под claim-ом.
    assert [c for c, _since in chat.read_channel_calls] == ["chan-1"]
    assert [e.id for e in intake.calls] == ["p-18"]
    assert len(chat.sent) == 1
