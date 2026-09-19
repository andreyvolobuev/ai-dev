# Design: создание Jira-задач по просьбе в Mattermost

Дата: 2026-09-19
Статус: согласован, готов к написанию плана реализации

## Проблема

Сотрудников команды регулярно просят о помощи соседние команды. Иногда это
3-4 часа в день. Эта работа не попадает ни в спринт, ни в какую-либо
отчётность, потому что заводить на неё тикет руками лень. Нужен способ
завести задачу одним сообщением в Mattermost, там же, где попросили.

## Область

В скоупе:

1. **Прямая просьба.** Бота тегают в канале или треде: «@ai-dev заведи мне
   задачу на сбор жёлтых карточек по Грузии». Бот создаёт тикет, описание —
   из самой просьбы.
2. **Просьба по треду.** Люди всё обсудили в треде, затем тегают бота:
   «@ai-dev прочитай тред и создай задачу». Бот читает транскрипт, из него
   пишет заголовок и описание.
3. **Правки по повторному упоминанию.** «@ai-dev исполнителем поставь Петю»,
   «@ai-dev переименуй в ...», «@ai-dev убери из спринта».

Каждый созданный тикет получает `labels: [dmp-sup]`, попадает в активный
спринт проекта, исполнителем становится автор просьбы (или тот, кого явно
назвали), в описании — суть просьбы и ссылка на тред-источник.

Вне скоупа:

* Общий ответчик на любые упоминания. Если упоминание не про создание или
  правку тикета — бот коротко отвечает, что занят, и больше в этот тред не
  лезет. Полноценный ответчик — отдельная задача.
* Любые статусные переходы созданного тикета, оценка, компоненты, epic link.
* Попадание созданных тикетов в собственный пайплайн бота (Analyst → Dev).
  Пайплайн забирает задачи по `labels = "ai-dev"`, `dmp-sup` его не триггерит —
  и это намеренно: задачи соседних команд делает человек.

## Ключевые решения

| Решение | Почему |
|---|---|
| Активный спринт ищем через `JQL project = X AND sprint in openSprints()`, id спринта берём из найденного тикета | Не требует знать id доски; доска может поменяться, JQL — нет |
| Исполнитель резолвится только по email (MM → Jira), без ручного маппинга | Ничего не нужно заполнять заранее; расхождение почт деградирует в «тикет без исполнителя» + честный ответ |
| Тикет создаётся сразу, без подтверждения; правки — повторным упоминанием | Главная боль — трение. Черновик с ожиданием «ок» добавляет ровно тот шаг, из-за которого задачи и не заводятся |
| Реагируем на упоминание в любом канале, где бот состоит | Раздавать доступ соседним командам = пригласить бота в канал |
| Побочные эффекты делает раннер, у модели только терминальный submit-тул | Модель читает недоверенный тред; прямой доступ к записи в Jira — это поверхность для prompt-injection |
| Интент определяет LLM, не regex | Форма просьбы произвольная; литеральный матч остаётся только на само упоминание `@<bot>` — это адресация, а не текст |
| Факты в ответе (ключ, ссылка, спринт, исполнитель) собирает раннер из шаблонов | Бот уже однажды сочинял факты о том, чего не делал, и защищал их перед людьми |

## Архитектура

Ничего нового в слоях не появляется — компоненты встают в существующие места:

```
MM WebSocket
   │
   ▼
runtime/workers/mm_thread_listener.py::_dispatch_inner
   │  (новый маршрут — последний в цепочке, но ДО выхода по "нет thread_root_id")
   ▼
application/agents/task_intake.py::TaskIntakeAgent      ← одна LLM-итерация
   │  submit_task_intake (tools/submit_task_intake.py, группа "intake")
   ▼
runtime/workers/intake_inbox.py::TaskIntakeInbox        ← все побочные эффекты
   ├─► domain/ports/task_tracker.py  (create_task / update_task / find_tracker_user_by_email)
   │       └─► adapters/task_tracker/jira.py
   ├─► infrastructure/db  (intake_requests)
   └─► application/services/communicator.py  (ответ в тред, reactive=True)
```

### Маршрутизация

`_dispatch_inner` проверяет маршруты по порядку: `/reset` → autofix-тред →
фрагмент ответа аналисту → **интейк** → ревью-тред MR.

Интейк срабатывает, когда в тексте поста есть `@<MATTERMOST_BOT_USERNAME>`.
Два требования к месту вставки:

* **До** существующего `if not event.thread_root_id: return` — иначе
  сценарий 1 (упоминание в корневом посте) не работает вообще.
* **Перед** маршрутом ревью-треда, но с явной уступкой ему: упоминание бота
  внутри ревью-треда MR обязано идти в `thread_responder`, а не в интейк.
  Поэтому первым делом интейк-маршрут проверяет, не принадлежит ли
  `thread_root_id` ревью-треду (`MergeRequestRow.review_thread_root_id`) или
  escalation-треду (`autofix_escalation_root_id`) — если да, пост уходит
  дальше по цепочке без обработки.

### Агент

`TaskIntakeAgent` — по образцу `ThreadResponderAgent`: один вызов
`CodeAgentPort` с единственной MCP-группой `intake` и без файловых
builtins (`Read`/`Glob`/`Grep` не нужны — ходить агенту некуда),
`max_turns = 4`.

Вход промпта:

* пост с упоминанием (автор, текст, время);
* транскрипт треда, если тред есть (весь, oldest-first);
* permalink на пост-источник;
* состояние уже созданного тикета треда, если он есть (ключ, заголовок,
  исполнитель, лейблы, спринт) — только для сценария правок.

Весь текст людей оборачивается `InjectionFilter`, как у аналиста и
респондера.

Выход — терминальный `submit_task_intake`:

```
action        ∈ {create, update, busy}
summary       строка   — заголовок тикета (русский, ≲120 символов)
description   строка   — суть просьбы; ссылку на тред и «кто просил» добавляет раннер
assignee_hint строка   — пусто = автор просьбы; иначе имя/хендл, как его назвали
changes       объект   — только для update: {summary?, description?, assignee_hint?, sprint?: bool}
reply_text    строка   — используется ТОЛЬКО при action=busy
reasoning     строка   — в лог и в agent_trace
```

Сценарии 1 и 2 не разведены в коде: вход одинаковый, различает их модель.
Явная развилка по формулировке ошибётся на третьем варианте фразы.

Модель — `agents.task_intake.model` (дефолт `default`). Если модель не
вызвала submit — поведение как у респондера: `busy`-подобный исход без
побочных эффектов, предупреждение в лог, ✅ на пост (чтобы не крутиться).

### Jira

Новые методы порта `TaskTrackerPort`:

```python
async def create_task(self, spec: NewTaskSpec) -> CreatedTask
async def update_task(self, external_id: str, patch: TaskPatch) -> None
async def find_tracker_user_by_email(self, email: str) -> str | None
```

`NewTaskSpec` / `TaskPatch` / `CreatedTask` — dataclasses в
`domain/models/task.py`. `NewTaskSpec`: project, issue_type, summary,
description, labels, assignee (tracker username | None),
add_to_active_sprint: bool. `CreatedTask`: key, url, assignee,
sprint_name | None, warnings: list[str] — раннер превращает warnings в
честный текст ответа.

Реализация в `JiraTaskTracker` (всё синхронное — через
`asyncio.to_thread`, как уже сделано в адаптере):

* **создание** — `create_issue(fields={project:{key}, issuetype:{name},
  summary, description, labels, assignee:{name}})`.
* **спринт** — `jql("project = <P> AND sprint in openSprints() ORDER BY updated DESC", limit=1)`;
  из найденного тикета читаем sprint-поле (его customfield id резолвим один
  раз через `get_all_fields()` по `name == "Sprint"` и кешируем на процесс);
  вытаскиваем id спринта; `add_issues_to_sprint(sprint_id, [key])`.
  Парсер sprint-поля понимает оба формата Jira Server: список словарей с
  `id`/`state` и легаси-строки
  `com.atlassian.greenhopper.service.sprint.Sprint@1a2b[id=567,state=ACTIVE,...]`;
  при нескольких значениях предпочитается `state=ACTIVE`.
* **исполнитель** — `user_find_by_user_string(username=<email>)` (Jira Server
  принимает именно `username`, матчит по username / displayName /
  emailAddress); берём запись с точным совпадением `emailAddress`,
  используем её `name`.
* **правки** — `edit_issue(key, fields)` для summary / description /
  assignee / labels; спринт — `add_issues_to_sprint` (добавить) или
  `edit_issue` со сбросом sprint-поля в `None` (убрать).

Деградация вместо падения — тикет существует, значит главная ценность
получена:

| Сбой | Поведение |
|---|---|
| Активный спринт не найден | Тикет создан без спринта, в warnings «активного спринта не нашла» |
| Jira-юзер по email не найден | Тикет создан без исполнителя, в warnings «не нашла тебя в Jira по почте» |
| `add_issues_to_sprint` упал после создания | Ключ и ссылка всё равно возвращаются, сбой в warnings |
| `create_issue` упал | Тикет не создан, в тред уходит `intake_failed` с короткой причиной (без стектрейса) |

### Хранение и идемпотентность

Миграция `migrations/versions/0014_intake_requests.py`:

```
intake_requests
  issue_key             text  PK
  mm_root_id            text  index      -- корень треда, где попросили
  mm_channel_id         text
  source_post_id        text  UNIQUE     -- пост с просьбой
  requester_mm_user_id  text
  created_at            timestamptz
```

`source_post_id UNIQUE` — основной страж от дублей. ✅-реакция ставится уже
после создания тикета, а повторная доставка одного поста (WebSocket +
catch-up sweep) реальна: строка вставляется до ответа в тред, вторая попытка
ловит `IntegrityError` и выходит молча. Существующие механизмы
(`_inflight_posts`, `ProcessedThreadPostRow`, ✅) остаются как есть.

`mm_root_id` для сценария 1 — это `event.thread_root_id or event.id`:
упоминание в корневом посте треда не имеет, но ответ бота его создаёт, и
последующие правки прилетают реплаями с этим же root_id.

Один тред может содержать несколько тикетов (повторная просьба «заведи ещё
одну» → новая строка). Правки применяются к последнему созданному тикету
треда.

**Правки — только по повторному упоминанию.** В сценарии 2 тред живёт своей
жизнью после создания тикета; трактовать каждый реплай как правку — это
гарантированные ложные срабатывания в чужом обсуждении.

### Permalink на тред

В `ChatPort` добавляется `post_permalink(post_id, channel_id) -> str | None`
(дефолтная реализация — `None`, чтобы тестовые фейки не ломались). MM-адаптер
строит `{MATTERMOST_URL}/{team_name}/pl/{post_id}`, резолвя
`channel_id → team_id → team_name` и кешируя результат по channel_id. Если
резолв не удался, описание тикета пишется без ссылки — тикет всё равно
создаётся.

### Ответы в тред

Факты собирает раннер из шаблонов `config/notifications.yaml`
(схема — `infrastructure/config/schema.py`), бот говорит о себе в женском
роде, как и во всех существующих шаблонах:

```yaml
mattermost:
  intake_created: "Завела [{key}]({url}) — «{summary}». Исполнитель: {assignee}{warnings_block}"
  intake_updated: "Готово: {changes}"
  intake_failed: "Не смогла завести задачу в Jira: {reason}. Повтори просьбу — попробую снова."
```

`warnings_block` и `changes` рендерит раннер: первый — из
`CreatedTask.warnings` (пустая строка, когда предупреждений нет), второй — из
фактически применённого патча («переназначила на @petrov», «переименовала»),
а не из текста модели. Тексты короткие — 1-3 предложения, без перечисления умений: длинные
простыни — главная претензия людей к боту.

Ответ уходит даже вне рабочих часов: `CommunicatorService._send` сейчас
глушит всё вне `working_hours`, поэтому добавляется параметр
`reactive: bool = False` — он обходит working-hours-гейт, но не рейт-лимит.
Человек только что лично дёрнул бота; молчание читается как «сломался».

## Конфигурация

`config/agents.yaml` (схема — `IntakeCfg` в `infrastructure/config/schema.py`):

```yaml
task_intake:
  enabled: true
  project: "DM"
  issue_type: "Task"
  labels: ["dmp-sup"]
  add_to_active_sprint: true
```

`agents.task_intake.model` — модель агента (дефолт `default`).
Промпт — `config/prompts/task_intake.md`, подхватывается существующим
`PromptsLoader`.

## Безопасность

* Модель не имеет доступа к записи в Jira — только терминальный submit-тул;
  всё остальное делает раннер по валидированной структуре.
* Весь человеческий текст оборачивается `InjectionFilter`.
* Инъекция в чужом треде максимум испортит заголовок и описание одного
  тикета: количество тикетов на пост ограничено `source_post_id UNIQUE`,
  правки — только к тикетам того же треда.
* Право просить = членство в канале, куда бота пригласили. Отдельного
  whitelist нет намеренно: смысл фичи в том, чтобы соседние команды могли
  этим пользоваться.

## Тестирование

Unit (фейки, как во всём проекте):

* `tests/unit/test_task_intake_agent.py` — create из одной просьбы, create из
  треда, update, busy; модель не вызвала submit → никаких побочных эффектов.
* `tests/unit/test_jira_create_task.py` — парсинг sprint-поля в обоих
  форматах Jira Server; активный спринт не найден → тикет создан без спринта;
  ассайни не найден → тикет без исполнителя; `add_issues_to_sprint` упал
  после создания → ключ всё равно возвращён с warning.
* `tests/unit/test_intake_routing.py` — упоминание в ревью-треде идёт в
  `thread_responder`, не в интейк; упоминание в корневом посте доходит до
  интейка; повторная доставка того же поста не создаёт второй тикет; реплай
  без упоминания в интейк-треде игнорируется; при `enabled: false` маршрут
  не срабатывает.
* `tests/unit/test_communicator_write.py` — дополняется кейсом
  `reactive=True`: вне рабочих часов сообщение уходит, рейт-лимит при этом
  продолжает действовать.

Миграция покрывается существующим `tests/integration/test_alembic.py`.

## Файлы

Новые:

* `src/virtual_dev/application/agents/task_intake.py`
* `src/virtual_dev/tools/submit_task_intake.py`
* `src/virtual_dev/runtime/workers/intake_inbox.py`
* `config/prompts/task_intake.md`
* `migrations/versions/0014_intake_requests.py`
* `tests/unit/test_task_intake_agent.py`, `test_jira_create_task.py`, `test_intake_routing.py`

Изменяемые:

* `src/virtual_dev/domain/ports/task_tracker.py` — три новых метода
* `src/virtual_dev/domain/models/task.py` — `NewTaskSpec`, `TaskPatch`, `CreatedTask`
* `src/virtual_dev/adapters/task_tracker/jira.py` — реализация + резолв спринта и юзера
* `src/virtual_dev/domain/ports/chat.py`, `adapters/chat/mattermost.py` — `post_permalink`
* `src/virtual_dev/application/services/communicator.py` — `reactive`
* `src/virtual_dev/runtime/workers/mm_thread_listener.py` — новый маршрут
* `src/virtual_dev/infrastructure/db/models.py` — `IntakeRequestRow`
* `src/virtual_dev/infrastructure/config/schema.py`, `config/agents.yaml`, `config/notifications.yaml`
* `src/virtual_dev/infrastructure/container.py` — проводка агента и инбокса
* `docs/ARCHITECTURE.md` — строка про новый маршрут и агента
