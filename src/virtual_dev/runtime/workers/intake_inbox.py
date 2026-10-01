"""TaskIntakeInbox — исполняет решение интейка задач.

Просьбу «заведи задачу» приносит ``MmThreadListener``, решение
принимает ``TaskIntakeAgent``; здесь оно превращается в факты:
строка-заявка в БД, тикет в Jira, лейбл, спринт, исполнитель и ответ в
тред. Такое разделение сознательное — модель читает недоверенный чат,
поэтому запись в Jira идёт не через неё.

Порядок в ``_create`` важен: claim в БД берётся ДО похода в Jira.
``intake_requests.source_post_id`` уникален, так что повторная доставка
одного поста (WS-событие + catch-up sweep) второй тикет не создаст. Если
Jira ответила ошибкой, claim снимается — иначе повтор просьбы молча
превратился бы в «уже обработано».

Частичные сбои (нет активного спринта, не нашли человека в Jira) не
отменяют тикет: задача уже существует — честный текст ответа говорит,
что доделать руками.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from loguru import logger
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from virtual_dev.application.agents.task_intake import (
    IntakeAction,
    IntakeDecision,
    IntakeTicketState,
    TaskIntakeAgent,
)
from virtual_dev.application.services.communicator import CommunicatorService
from virtual_dev.domain.models.chat import ChatMessage, ChatUser
from virtual_dev.domain.models.task import NewTaskSpec, TaskPatch
from virtual_dev.domain.ports.chat import ChatPort
from virtual_dev.domain.ports.task_tracker import TaskTrackerPort
from virtual_dev.infrastructure.config import AppConfig, MmTemplatesCfg
from virtual_dev.infrastructure.db import IntakeRequestRow

# Код предупреждения -> ключ шаблона в notifications.mattermost.
_WARNING_TEMPLATES: dict[str, str] = {
    "no_active_sprint": "intake_warning_no_active_sprint",
    "sprint_failed": "intake_warning_sprint_failed",
    "assignee_not_found": "intake_warning_assignee_not_found",
    "assignee_hint_unresolved": "intake_warning_assignee_hint_unresolved",
    # Правка тикета не назначает исполнителя автору просьбы (в отличие от
    # создания) — здесь текст другой: "не поняла, оставила как было"
    # вместо "поставила тебя".
    "assignee_hint_unresolved_update": "intake_warning_assignee_hint_unresolved_update",
}

# Предел длины отказа «занята». Текст пишет модель, прочитавшая чужой
# тред, и он уходит в командный канал как есть — поэтому и кап, и
# подмена шаблоном: длинная простыня ломает голос бота (1-3 фразы) и
# превращает бота в рупор для инъекции.
_MAX_BUSY_REPLY_CHARS = 300


@dataclass
class IntakeOutcome:
    """Чем закончилась обработка одного упоминания."""

    action: str                      # created | updated | busy | skipped | failed
    issue_key: str | None = None
    reply_sent: bool = False
    reason: str = ""
    warnings: list[str] = field(default_factory=list)


class TaskIntakeInbox:
    def __init__(
        self,
        *,
        agent: TaskIntakeAgent,
        task_tracker: TaskTrackerPort | None,
        chat: ChatPort,
        communicator: CommunicatorService,
        session_factory: async_sessionmaker[AsyncSession],
        config: AppConfig,
    ) -> None:
        self._agent = agent
        self._tracker = task_tracker
        self._chat = chat
        self._communicator = communicator
        self._session_factory = session_factory
        self._config = config

    async def handle(self, event: ChatMessage) -> IntakeOutcome:
        cfg = self._config.agents.task_intake
        if not cfg.enabled:
            return IntakeOutcome(action="skipped", reason="disabled")
        if self._tracker is None:
            logger.warning("TaskIntake: no task tracker configured — ignoring ask")
            return IntakeOutcome(action="skipped", reason="no_tracker")

        # Дешёвая проверка ДО модели: повторная доставка одного и того же
        # поста (WS-событие + catch-up sweep) не должна ни звать агента
        # заново, ни постить второй ответ — тикет по нему уже (или вот-вот
        # будет) заведён. Атомарная гарантия остаётся на UNIQUE-инсерте в
        # ``_claim``; это только фильтр перед ним.
        if await self._already_claimed(event.id):
            logger.info(
                "TaskIntake: post {} already claimed — skipping re-delivery",
                event.id,
            )
            return IntakeOutcome(action="skipped", reason="duplicate_post")

        # Пост без треда сам становится корнем: ответ бота создаст тред,
        # и правки прилетят реплаями внутри него — тот же root_id.
        root_id = event.thread_root_id or event.id
        thread = await self._read_thread(root_id, event)
        permalink = await self._safe_permalink(event.id, event.channel_id)
        existing = await self._existing_ticket(root_id)

        decision = await self._agent.decide(
            post=event, thread=thread, permalink=permalink, existing=existing,
        )

        if decision.action is IntakeAction.BUSY:
            sent = await self._reply(event, root_id, self._busy_text(decision.reply_text))
            return IntakeOutcome(action="busy", reply_sent=sent)

        if decision.action is IntakeAction.UPDATE:
            return await self._update(event, root_id, existing, decision)

        return await self._create(event, root_id, permalink, decision)

    # --- create ---

    async def _create(
        self,
        event: ChatMessage,
        root_id: str,
        permalink: str,
        decision: IntakeDecision,
    ) -> IntakeOutcome:
        assert self._tracker is not None
        claim_id = await self._claim(event, root_id)
        if claim_id is None:
            return IntakeOutcome(action="skipped", reason="duplicate_post")

        requester = await self._chat.get_user_by_id(event.author_id)
        requester_label = _requester_label(requester, event.author_id)
        assignee, warnings = await self._resolve_assignee(
            requester_email=(requester.email if requester else None),
            hint=decision.assignee_hint,
        )

        cfg = self._config.agents.task_intake
        spec = NewTaskSpec(
            project=cfg.project,
            issue_type=cfg.issue_type,
            summary=decision.summary,
            description=_compose_description(
                decision.description,
                requester_label=requester_label,
                permalink=permalink,
            ),
            labels=list(cfg.labels),
            assignee=assignee,
            add_to_active_sprint=cfg.add_to_active_sprint,
        )
        try:
            created = await self._tracker.create_task(spec)
        except Exception as exc:
            logger.exception("TaskIntake: create_task failed for post {}", event.id)
            await self._release(claim_id)
            sent = await self._reply(
                event, root_id,
                self._templates.intake_failed.format(reason=_public_cause(exc)),
            )
            return IntakeOutcome(action="failed", reply_sent=sent)

        try:
            await self._store_key(claim_id, created.key)
        except Exception:
            # Тикет уже создан в Jira. Теряем только возможность найти
            # тикет по треду для будущих правок.
            logger.exception(
                "TaskIntake: failed to persist issue_key {} for claim {}",
                created.key, claim_id,
            )
        all_warnings = [*created.warnings, *warnings]
        text = self._templates.intake_created.format(
            key=created.key,
            url=created.url,
            summary=decision.summary,
            assignee=created.assignee or "не назначен",
            sprint=created.sprint_name or "без спринта",
            warnings_block=self._render_warnings(all_warnings),
        )
        sent = await self._reply(event, root_id, text)
        logger.info(
            "TaskIntake: created {} for @{} (warnings={})",
            created.key, event.author_id, all_warnings,
        )
        return IntakeOutcome(
            action="created", issue_key=created.key,
            reply_sent=sent, warnings=all_warnings,
        )

    # --- update ---

    async def _update(
        self,
        event: ChatMessage,
        root_id: str,
        existing: IntakeTicketState | None,
        decision: IntakeDecision,
    ) -> IntakeOutcome:
        assert self._tracker is not None
        if existing is None:
            sent = await self._reply(
                event, root_id,
                self._templates.intake_failed.format(
                    reason="не нашла тикет, который надо поправить",
                ),
            )
            return IntakeOutcome(action="failed", reply_sent=sent, reason="no_ticket")

        changes = decision.changes
        patch = TaskPatch()
        applied: list[str] = []
        warnings: list[str] = []

        summary = str(changes.get("summary") or "").strip()
        if summary:
            patch.summary = summary
            applied.append(f"переименовала в «{summary}»")
        description = str(changes.get("description") or "").strip()
        if description:
            # Патч описания в Jira заменяет поле целиком, но футер
            # («Просьба от», «Обсуждение») дописываем мы — значит надо
            # собрать заново, иначе одно «допиши про Армению» уносит
            # ссылку на тред-источник, которую гарантирует спека.
            patch.description = await self._recompose_description(
                description, issue_key=existing.key,
            )
            applied.append("обновила описание")
        hint = str(changes.get("assignee_hint") or "").strip()
        hint_unresolved = False
        if hint:
            username = await self._resolve_named_user(hint)
            if username:
                patch.assignee = username
                applied.append(f"переназначила на {username}")
            else:
                # Отдельный код от "assignee_hint_unresolved": на создании
                # неразрешённая подсказка означает "поставила тебя", тогда
                # как на правке исполнителя вообще не тронули.
                warnings.append("assignee_hint_unresolved_update")
                hint_unresolved = True
        # Только настоящий bool. SDK не валидирует JSON Schema тула, так что
        # модель спокойно пришлёт "sprint": null как заполнитель «не
        # меняем»; bool(None) выкинул бы тикет из спринта, и бот бы
        # отрапортовал «убрала из спринта», хотя никто не просил. Строка
        # "false" ошиблась бы в другую сторону. Видимость в спринте — весь
        # смысл фичи, поэтому неоднозначное значение игнорируем.
        raw_sprint = changes.get("sprint")
        if isinstance(raw_sprint, bool):
            patch.sprint = raw_sprint
            applied.append(
                "вернула в спринт" if raw_sprint else "убрала из спринта"
            )
        elif "sprint" in changes:
            logger.warning(
                "TaskIntake: ignoring non-boolean sprint change {!r} for {}",
                raw_sprint, existing.key,
            )

        if patch.is_empty():
            # Если единственной запрошенной правкой был исполнитель и подсказку
            # не удалось разрешить — ответ должен называть настоящую причину,
            # не общее "не поняла, что именно поправить".
            reason = (
                self._templates.intake_warning_assignee_hint_unresolved_update
                if hint_unresolved
                else "не поняла, что именно поправить"
            )
            sent = await self._reply(
                event, root_id,
                self._templates.intake_failed.format(reason=reason),
            )
            return IntakeOutcome(
                action="failed", issue_key=existing.key,
                reply_sent=sent, reason="empty_patch",
            )

        try:
            await self._tracker.update_task(existing.key, patch)
        except Exception as exc:
            logger.exception("TaskIntake: update_task failed for {}", existing.key)
            sent = await self._reply(
                event, root_id,
                self._templates.intake_failed.format(reason=_public_cause(exc)),
            )
            return IntakeOutcome(
                action="failed", issue_key=existing.key, reply_sent=sent,
            )

        text = self._templates.intake_updated.format(changes="; ".join(applied))
        block = self._render_warnings(warnings)
        sent = await self._reply(event, root_id, f"{text.rstrip()}{block}")
        return IntakeOutcome(
            action="updated", issue_key=existing.key,
            reply_sent=sent, warnings=warnings,
        )

    # --- helpers ---

    @property
    def _templates(self) -> MmTemplatesCfg:
        return self._config.notifications.mattermost

    async def _read_thread(
        self, root_id: str, event: ChatMessage,
    ) -> Sequence[ChatMessage]:
        try:
            thread = list(await self._chat.read_thread(root_id))
        except Exception:
            logger.warning("TaskIntake: read_thread({}) failed", root_id)
            return []
        # Само упоминание уходит в промпт отдельным блоком — в транскрипте
        # оно только дублировалось бы.
        return [msg for msg in thread if msg.id != event.id]

    async def _safe_permalink(self, post_id: str, channel_id: str) -> str:
        try:
            return await self._chat.post_permalink(post_id, channel_id) or ""
        except Exception:
            logger.warning("TaskIntake: permalink for post {} failed", post_id)
            return ""

    def _busy_text(self, reply_text: str) -> str:
        """Текст отказа «занята».

        Прозу модели в канал пускаем только короткой: пустую и слишком
        длинную заменяем своим шаблоном (см. ``_MAX_BUSY_REPLY_CHARS``).
        """
        text = reply_text.strip()
        if not text:
            return self._templates.intake_busy_fallback
        if len(text) > _MAX_BUSY_REPLY_CHARS:
            logger.warning(
                "TaskIntake: busy reply_text is {} chars (cap {}) — posting the "
                "template instead: {!r}",
                len(text), _MAX_BUSY_REPLY_CHARS, text[:200],
            )
            return self._templates.intake_busy_fallback
        return text

    async def _recompose_description(self, body: str, *, issue_key: str) -> str:
        """Описание тикета заново: текст от модели + наш футер.

        Заказчика и ссылку на тред восстанавливаем из сохранённой заявки —
        модель их не знает, но патч описания в Jira перезаписывает поле
        целиком.
        """
        row = await self._origin_row(issue_key)
        if row is None:
            logger.warning(
                "TaskIntake: no intake row for {} — description footer will be "
                "rebuilt without the source link", issue_key,
            )
            return _compose_description(body, requester_label="", permalink="")
        requester: ChatUser | None = None
        try:
            requester = await self._chat.get_user_by_id(row.requester_mm_user_id)
        except Exception:
            logger.warning(
                "TaskIntake: get_user_by_id({}) failed while rebuilding the "
                "description", row.requester_mm_user_id,
            )
        return _compose_description(
            body,
            requester_label=_requester_label(requester, row.requester_mm_user_id),
            permalink=await self._safe_permalink(
                row.source_post_id, row.mm_channel_id,
            ),
        )

    async def _resolve_assignee(
        self, *, requester_email: str | None, hint: str,
    ) -> tuple[str | None, list[str]]:
        """``(логин в трекере, предупреждения)``.

        Именованный человек — через поиск в чате (там есть ФИО), дальше по
        email в трекер; если никого не нашли или нашли нескольких —
        ставим автора просьбы: потерять исполнителя лучше, чем назначить
        чужого.
        """
        assert self._tracker is not None
        warnings: list[str] = []
        if hint:
            username = await self._resolve_named_user(hint)
            if username:
                return username, warnings
            warnings.append("assignee_hint_unresolved")
        if not requester_email:
            warnings.append("assignee_not_found")
            return None, warnings
        username = await self._safe_find_by_email(requester_email)
        if username is None:
            warnings.append("assignee_not_found")
        return username, warnings

    async def _resolve_named_user(self, name: str) -> str | None:
        assert self._tracker is not None
        try:
            hits = list(await self._chat.search_users_by_name(name, limit=5))
        except Exception:
            logger.warning("TaskIntake: chat user search for {!r} failed", name)
            return None
        with_email = [u for u in hits if u.email]
        if len(with_email) != 1:
            return None
        email = with_email[0].email or ""
        return await self._safe_find_by_email(email)

    async def _safe_find_by_email(self, email: str) -> str | None:
        """``find_tracker_user_by_email`` raises loudly on transport/auth
        errors (real Jira adapter) — a Jira 5xx here must degrade into
        "assignee not found", never abort ticket creation entirely."""
        assert self._tracker is not None
        try:
            return await self._tracker.find_tracker_user_by_email(email)
        except Exception:
            logger.warning(
                "TaskIntake: find_tracker_user_by_email({!r}) failed", email,
            )
            return None

    def _render_warnings(self, codes: Sequence[str]) -> str:
        phrases: list[str] = []
        for code in codes:
            key = _WARNING_TEMPLATES.get(code)
            phrase = (getattr(self._templates, key, "") if key else "").strip()
            if phrase and phrase not in phrases:
                phrases.append(phrase)
        if not phrases:
            return ""
        return "\n\n" + "; ".join(phrases) + "."

    async def _reply(self, event: ChatMessage, root_id: str, text: str) -> bool:
        outcome = await self._communicator.send_channel(
            event.channel_id, text.strip(), thread_root_id=root_id, reactive=True,
        )
        return outcome.sent

    # --- storage ---

    async def _already_claimed(self, source_post_id: str) -> bool:
        """Cheap pre-check for a re-delivered post — before the agent runs.

        The unique insert in ``_claim`` is the atomic guarantee against a
        race between two concurrent deliveries; this is just the guard in
        front of it so a *sequential* re-delivery (WS event, then the same
        post again via catch-up sweep) neither re-calls the model nor
        re-evaluates update/busy against the ticket the first delivery
        already created.
        """
        async with self._session_factory() as session:
            stmt = (
                select(IntakeRequestRow.id)
                .where(IntakeRequestRow.source_post_id == source_post_id)
                .limit(1)
            )
            return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def _claim(self, event: ChatMessage, root_id: str) -> int | None:
        async with self._session_factory() as session:
            row = IntakeRequestRow(
                source_post_id=event.id,
                mm_root_id=root_id,
                mm_channel_id=event.channel_id,
                requester_mm_user_id=event.author_id,
            )
            session.add(row)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                logger.info(
                    "TaskIntake: post {} already claimed — not creating a "
                    "second ticket", event.id,
                )
                return None
            claim_id = int(row.id)
            await session.commit()
            return claim_id

    async def _release(self, claim_id: int) -> None:
        async with self._session_factory() as session:
            await session.execute(
                delete(IntakeRequestRow).where(IntakeRequestRow.id == claim_id)
            )
            await session.commit()

    async def _store_key(self, claim_id: int, issue_key: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(IntakeRequestRow)
                .where(IntakeRequestRow.id == claim_id)
                .values(issue_key=issue_key)
            )
            await session.commit()

    async def _origin_row(self, issue_key: str) -> IntakeRequestRow | None:
        """Заявка, по которой этот тикет и был заведён (самая первая).

        Нужна, чтобы восстановить футер описания: автора просьбы и
        ссылку на пост-источник.
        """
        async with self._session_factory() as session:
            stmt = (
                select(IntakeRequestRow)
                .where(IntakeRequestRow.issue_key == issue_key)
                .order_by(IntakeRequestRow.id.asc())
                .limit(1)
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    async def _existing_ticket(self, root_id: str) -> IntakeTicketState | None:
        """Последний тикет, заведённый по этому треду (для правок)."""
        async with self._session_factory() as session:
            stmt = (
                select(IntakeRequestRow)
                .where(
                    IntakeRequestRow.mm_root_id == root_id,
                    IntakeRequestRow.issue_key.is_not(None),
                )
                .order_by(IntakeRequestRow.id.desc())
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None or row.issue_key is None:
            return None
        key = row.issue_key
        if self._tracker is None:
            return IntakeTicketState(key=key)
        try:
            task = await self._tracker.get_task(key)
        except Exception:
            logger.warning("TaskIntake: could not refresh {} from tracker", key)
            return IntakeTicketState(key=key)
        return IntakeTicketState(
            key=key,
            summary=task.title,
            assignee=task.assignee_id,
            labels=list(task.labels),
        )


def _compose_description(
    body: str, *, requester_label: str, permalink: str,
) -> str:
    """Тело тикета: текст от модели + факты от нас.

    Ссылка на тред и имя заказчика приписываются здесь — не моделью:
    она их не знает, и угадывать такое нельзя.
    """
    parts = [body.strip() or "(без описания)", ""]
    if requester_label:
        parts.append(f"Просьба от: {requester_label}")
    if permalink:
        parts.append(f"Обсуждение: {permalink}")
    parts.append("")
    parts.append("Задачу завела Аида Нейронова по просьбе в Mattermost.")
    return "\n".join(parts)


def _requester_label(user: ChatUser | None, fallback_id: str) -> str:
    return f"@{user.username}" if user and user.username else fallback_id


# Причина отказа для канала. Текст исключения туда уходить не должен:
# ``requests.HTTPError`` несёт полный внутренний REST-URL, но канал
# читают соседние команды. Подробности остаются в логе.
_CAUSE_UNAVAILABLE = "Jira не ответила"
_CAUSE_FORBIDDEN = "нет прав"
_CAUSE_NOT_FOUND = "не нашла проект"
_CAUSE_REJECTED = "Jira не приняла поля задачи"
_CAUSE_UNKNOWN = "что-то сломалось на стороне Jira"


def _public_cause(exc: Exception) -> str:
    """Короткая человеческая причина для ответа в тред.

    Маппинг живёт в коде, не в ``config/notifications.yaml``: это
    классификация исключений, не настраиваемый текст.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    cause = _CAUSE_UNKNOWN
    if isinstance(status, int):
        if status in (401, 403):
            cause = _CAUSE_FORBIDDEN
        elif status == 404:
            cause = _CAUSE_NOT_FOUND
        elif status >= 500:
            cause = _CAUSE_UNAVAILABLE
        elif status >= 400:
            cause = _CAUSE_REJECTED
    elif isinstance(exc, (TimeoutError, OSError)):
        # requests.ConnectionError / requests.Timeout наследуют OSError —
        # обрыв связи и таймаут для человека это одно и то же.
        cause = _CAUSE_UNAVAILABLE
    logger.warning(
        "TaskIntake: tracker error reported as {!r} — {}: {}",
        cause, type(exc).__name__, " ".join(str(exc).split()),
    )
    return cause


__all__ = ["IntakeOutcome", "TaskIntakeInbox"]
