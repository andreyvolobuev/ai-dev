# Контекст проекта Virtual Dev

## Что это
Мульти-агентный AI-разработчик для команды DataMining (2GIS).
Подробнее — в README.md (появится на следующем шаге).

## История принятых решений

### Стек
- Python 3.13+ (на моей машине 3.14, но `requires-python = ">=3.13"` для совместимости зависимостей)
- Менеджер пакетов: uv
- LLM: Claude Sonnet 4.5 (основная), Haiku 4.5 (для лёгких задач типа "суммаризируй тред")
- Агентный фреймворк: **Claude Agent SDK** (`claude-agent-sdk` на PyPI) — обёртка над `claude` CLI, работает через залогиненную Claude Max сессию. **API-ключ не используем**, SDK `anthropic` в зависимостях нет. См. «LLM-инфра» ниже.
- Трекер: Jira (self-hosted 2GIS)
- VCS: GitLab (self-hosted 2GIS)
- Чат: Mattermost (self-hosted 2GIS)
- KB: Confluence (self-hosted 2GIS)
- БД на старте: SQLite
- Web-дашборд: FastAPI + Jinja2
- CLI: typer

### LLM-инфра (важно!)
- Бот работает **через Claude Max подписку пользователя**, не через Anthropic API.
- Все вызовы модели идут через `claude-agent-sdk` → subprocess `claude` (из PATH) → уже залогиненный Claude Code на машине.
- `ANTHROPIC_API_KEY` нигде не ставим, пакет `anthropic` в зависимостях НЕ нужен.
- **Нет budget-лимитов в долларах и токенах:** у Max нет per-token/per-dollar биллинга. Не добавлять `PER_TASK_BUDGET_USD`, `max_tokens_per_turn`, `max_budget_usd` и подобное.
- Единственный лимит — `max_iterations_per_task` (он же `ClaudeAgentOptions.max_turns`): защита от runaway-циклов, не от денег.
- `plans.cost_usd` в БД — это оценочная цифра из `ResultMessage.total_cost_usd`, хранится как информация для аналитики; ничего на ней не enforce'ится и наружу не показывается.
- Rate-limit Max'а (сообщений в 5-часовое окно) — SDK выдаёт `RateLimitEvent`, обработаем backoff'ом, не cost-логикой.

### Архитектура — hexagonal (ports & adapters)
Слои:
- `domain/` — модели и интерфейсы (ports). Без внешних зависимостей.
- `application/` — агенты, workflows, services. Зависят только от портов.
- `adapters/` — конкретные реализации портов (Jira, GitLab, Mattermost, Anthropic, ...).
- `infrastructure/` — БД, конфиг, DI, логи.
- `presentation/` — web-дашборд, CLI, webhooks.
- `runtime/` — воркеры, scheduler.

Смысл слоёв: чтобы завтра поменять Mattermost на Slack, Jira на Trello, облачный Claude на self-hosted Llama — надо менять ТОЛЬКО адаптер, не трогая domain и application.

### Агенты (мульти-агентная архитектура, каждый — отдельная сессия Claude Agent SDK со своим контекстом)
- **Orchestrator** — маршрутизирует задачи, эскалирует, держит только метаданные.
- **Analyst** — читает тикет + Confluence + MM-треды, строит план.
- **Researcher** — RAG по коду / истории MR / Confluence по запросу других агентов.
- **Communicator** — ЕДИНСТВЕННЫЙ, кто пишет в Mattermost. Фильтрует injection.
- **Dev (N штук)** — по одному на (репо, специализация: backend/frontend/devops). У каждого своя "книга правил".
- **Reviewer** — обрабатывает комменты в открытых MR.
- **QA** — валидация тестов.
- **DevOps** — CI/CD, красные пайплайны.

Агенты общаются через message bus (SQLite-таблица `messages` на старте, в будущем можно Redis/RabbitMQ).

### Репозитории
На старте — только `bellingshausen` (уже склонирован в `/Users/andreyvolobuev/Documents/2gis/bellingshausen`). Это монорепа, backend + frontend. Остальные 6 — подключим потом через `config/repositories.yaml`.

Все 7 репо:
- git@gitlab.2gis.ru:sd-data-mining/bellingshausen.git (backend + frontend)
- git@gitlab.2gis.ru:sd-data-mining/rainbow.git (backend)
- git@gitlab.2gis.ru:sd-data-mining/pts-aggregator.git (backend)
- git@gitlab.2gis.ru:sd-data-mining/greeder.git (backend)
- git@gitlab.2gis.ru:sd-data-mining/alertilka/alertilka-backend.git (backend)
- git@gitlab.2gis.ru:sd-data-mining/alertilka/alertilka-deploy.git (devops only)
- git@gitlab.2gis.ru:sd-data-mining/alertilka/alertilka-ui.git (frontend)

Репо добавляются динамически: строчка в `config/repositories.yaml` → перезапуск → появляются агенты.

### Общение с людьми (правила)
- Рабочие часы: 10:00–20:00 Мск, пн-пт.
- В первом сообщении треда/личке — дисклеймер про бота и команду `!ALARM` для остановки.
- Не дублировать дисклеймер в каждом сообщении.
- Кого спрашивать: вопросы по коду → git blame → автор; вопросы по бизнесу → командный канал.
- Timeout 4 часа в рабочее время → эскалация человеку (тимлиду).

### Безопасность
- Все входные данные от людей = untrusted, пропускаются через injection-фильтр (`InjectionFilter` оборачивает в `<untrusted_content>`).
- Белый список репо в конфиге.
- Защита от runaway-цикла: `max_iterations_per_task` в `config/agents.yaml` (15 для analyst, 30 для developer).
- Kill-switch в дашборде.
- Секреты: на старте `.env`, потом Vault (есть в компании).

### JQL для отбора задач
Конфигурируемый. На старте:
`assignee = currentUser() AND labels = "ai-dev" AND status = "To Do"`
(пользователь пока использует свой аккаунт, без отдельного bot-юзера).

### Jira workflow
`To Do → In Progress → Review → Testing → Done`

### Самообучение
Markdown-файлы `config/rules/<agent>.md`. Подкладываются в system prompt. После задач агент предлагает дополнения, одобряет человек. Раз в неделю — дайджест тимлиду.

### Политика ревью
- Мержит человек (не бот) — осознанное решение.
- Бот пишет в канал "гляньте MR", пингует неотреагировавших.
- Когда собрал N апрувов — пишет "апрувы собрал, прошу смержить".

## Что уже сделано

### Фаза 0 — СДЕЛАНО ✅
- Структура проекта, `pyproject.toml`, `.gitignore`, `.env.example`, `config/*.yaml`, `config/rules/*.md`.
- Все domain-модели (`Task, Repository, ChatMessage, MergeRequest, KBPage, Plan`).
- Все 8 ports (`TaskTrackerPort, VcsPort, ChatPort, KnowledgeBasePort, LlmPort, CodeAgentPort, SecretsPort, MessageBusPort`).
- Адаптер Jira (`JiraTaskTracker`) через `atlassian-python-api`.
- Infrastructure: pydantic-settings, yaml-loader с мерджем `local.yaml`, async SQLAlchemy 2.0, ORM-модели (TaskRow, MergeRequestRow, AgentMessageRow, EventRow, PlanRow), DI-container, loguru-setup.
- Минимальный Orchestrator + FastAPI-дашборд + typer CLI (`db init`, `run`, `poll-once`).
- `docs/ARCHITECTURE.md`.

### Фаза 1 — СДЕЛАНО ✅
- **Analyst** (`application/agents/analyst.py`): подписан на `task.discovered` через message bus, собирает контекст (Jira desc + MM-треды через read-only Communicator + опционально Confluence), пропускает через InjectionFilter, зовёт Claude через `claude-agent-sdk` с двумя in-process MCP-серверами (Researcher tools + private `submit_plan`), получает структурированный Plan, сохраняет в БД, обновляет internal_status (READY / CLARIFYING / FAILED). Идемпотентность: повторная обработка того же тикета = no-op при свежем плане.
- **Researcher** (`application/services/researcher.py`): in-process MCP-сервер с тулами `search_code` (git grep), `read_file`, `kb_search`, `kb_fetch_page_by_url`. Результаты всех тулов оборачиваются в `<untrusted_content>`. **RAG по истории MR — ещё нет**, это Phase 2 (для Dev-агента).
- **Communicator** (`application/services/communicator.py`): **read-only**. `digest_thread(url)` читает тред из MM, рендерит и оборачивает через InjectionFilter. `send_*` методы в `MattermostChat` бросают `NotImplementedError` — Phase 3 включит запись.
- **Injection filter** (`application/services/injection_filter.py`): `<untrusted_content>`-обёртка с disarmed closing-тегом, санитайз zero-width/bidi/tag-unicode, регексы на 5 классов инъекций (override, role-play-takeover, prompt-boundary, tool-call-forgery, jailbreak) → notes вне блока.
- **SqliteMessageBus** (`adapters/message_bus/sqlite.py`): durable шина через `agent_messages` table. Single-consumer per `to_agent`, atomic claim через stamped `consumed_at`, `"*"` broadcast фанаутит на известных подписчиков. Продакшн-дефолт; `InMemoryMessageBus` оставлен для тестов.
- **Claude Agent SDK адаптеры** (`adapters/code_agent/claude_sdk.py`, `adapters/llm/claude_sdk.py`): через `claude-agent-sdk.query()`. MCP servers и allowed_tools передаются через `CodeAgentRequest.extras`. Бюджетов/токенов нет (см. «LLM-инфра»).
- **Mattermost adapter** (`adapters/chat/mattermost.py`): read_thread + find_user_by_*; send_* и subscribe — NotImplementedError (Phase 3).
- **Confluence adapter** (`adapters/knowledge_base/confluence.py`): fetch_page, fetch_page_by_url (парсит URL трёх видов), search (CQL).
- **AnalystInbox** (`runtime/workers/analyst_inbox.py`): handler на `task.discovered`. Optimistic transition Jira To Do → In Progress, потом `AnalystAgent.handle_task()`, потом Jira-комментарий со сводкой плана (если план не FAILED). Каждый side-effect в своём try/except.
- **AgentRunner** (`runtime/workers/agent_runner.py`): generic subscribe-and-dispatch цикл на одного агента. Клеит подписку на шину с таблицей handler'ов по topic.
- **Orchestrator**: теперь публикует `task.discovered` на шину после commit'а новой задачи (обновления — нет).
- **Dashboard**: `/plans` список, секция планов на `/tasks/{id}`, healthz показывает статусы всех адаптеров (Jira/MM/KB/Analyst/Orchestrator).
- **CLI**: `virtual-dev plan-task DM-1234 [--post]` — прогнать Analyst локально, флаг `--post` включает запись в Jira.
- **Тесты**: 44 unit (Plan domain, SqliteBus, InjectionFilter, link extractor, Communicator, Researcher, Analyst c подменой `_call_model`, Orchestrator publish, AgentRunner). `claude-agent-sdk` в тестах не запускается — фейковый CodeAgentPort.

### Поправки/коррекции в ходе Phase 1 (важно)
- Удалили из кода все "API-мышление": `ANTHROPIC_API_KEY`, `PER_TASK_BUDGET_USD`, `DAILY_BUDGET_USD`, `max_tokens_per_turn`, `max_budget_usd`, `temperature`. У Max этого нет.
- `MessageBusPort` — теперь durable SQLite, не in-memory (все агенты через шину — архитектурное требование).
- Rule-файлы `config/rules/<agent>.md` — пока лежат как есть, но в system prompt агентов пока не подкладываются. Это делается в Phase 2 (для Dev-агента — обязательно).

### Фаза 2 — СДЕЛАНО ✅
- **VcsPort расширен** методами `current_branch`, `has_uncommitted_changes` (safety-хуки).
- **`GitLabVcs` адаптер** (`adapters/vcs/gitlab.py`): локальные git-операции через `subprocess`, удалённые — через `python-gitlab`. Commits с bot identity через per-call `-c user.name/email` (никаких глобальных мутаций git config). Workspace: если в `repositories.yaml` указан `local_path` — используется он (reuse пользовательского чекаута), иначе `{workspaces_dir}/{repo_key}/`. При использовании `local_path` — safety-check на чистоту дерева на входе (один раз за процесс), чтобы `reset --hard` / `checkout -B` не затёр несохранённые изменения. `fetch_and_checkout` делает hard reset на `origin/<branch>`.
- **Bot identity** в `.env`: `DEV_GIT_AUTHOR_NAME="Virtual Dev"`, `DEV_GIT_AUTHOR_EMAIL`, `DEV_BRANCH_PREFIX="ai-dev"`, `DEV_MR_DRAFT=true`.
- **`RulesLoader`** (`application/services/rules.py`): читает `config/rules/<agent_key>.md`, если нет — возвращает `""`. Splice'ится в system prompt Dev-агента.
- **`DevAgent`** (`application/agents/dev.py`): подписан на `plan.ready` для конкретного `(repo, specialisation)` ключа. Pre-check: задача есть + READY план + `dor_satisfied` + `target_repo_key` совпадает. Готовит workspace (ensure_clone + create_branch). Запускает Claude Agent SDK в `cwd=workspace` с полным набором Read/Glob/Grep/Edit/Write/Bash + приватный MCP `submit_mr`. После submit: commit → push → create_merge_request (draft по дефолту). 4 исхода: `SKIPPED`, `NO_CHANGES`, `MR_OPENED`, `FAILED`. Каждый с переходом `TaskStatus` и записью в `MergeRequestRow`.
- **AnalystInbox** теперь публикует `plan.ready` на шину для Dev-агента, если `plan.status == READY` и `target_repo_key` определён. Адресуется ключу `dev-<repo>-<specialisation>` (по умолчанию `backend`).
- **`DevInbox`** (`runtime/workers/dev_inbox.py`): handler `plan.ready` per-Dev-agent. На `MR_OPENED`: Jira transition `In Progress → Review` + коммент с ссылкой на MR. На `FAILED`/`NO_CHANGES`: коммент с notes. На `SKIPPED`: тихо (info log).
- **Автономный цикл** от `ai-dev` метки в Jira до draft MR в GitLab — без ручного клика на каждый тикет. Единственный ручной шлюз на входе — наличие метки `labels = "ai-dev"` (это JQL-фильтр Orchestrator'а). Выходной шлюз — ревью и мёрж MR человеком.
- **Dashboard**: секция MR на `/tasks/{id}`, список `/mrs`, в healthz — статусы всех Dev-раннеров.
- **CLI**: `virtual-dev dev-task DM-1234 --repo bellingshausen [--post]` — прогнать Dev-агента локально.
- **Тесты**: 65 unit. Новое: 6 GitLabVcs-локальных (на реальном tmp git-репо c `receive.denyCurrentBranch=updateInstead`), 9 DevAgent (все outcome'ы + rules injection + branch naming), 6 на handoff Analyst→Dev. `claude-agent-sdk` в тестах не запускается — фейковый CodeAgentPort.
- **Docs**: `docs/ARCHITECTURE.md` обновлён (data flow Phase 2, safety rails с human gate, workspace isolation, bot identity).

### Решения в Phase 2, которых не было в CONTEXT.md — утвердили с пользователем
- DevAgent **уважает** `local_path` из `repositories.yaml` (как Researcher). Если указан — работает в пользовательском чекауте (с safety-check на uncommitted changes). Иначе клонирует в `{workspaces_dir}/{repo_key}/`. Было иначе (отдельный клон всегда), поменяли в ходе smoke-теста.
- Коммиты: автор `Virtual Dev <virtual-dev@datamining.2gis.ru>`, push под твоим GitLab token.
- Ветки: `ai-dev/<external_id>-<slug>`.
- Task gate для Dev: `plan.status=READY` + `target_repo_key` установлен. Человеческий гейт — только метка `ai-dev` в Jira (на входе) и ревью MR (на выходе). Поле `task.dor_satisfied` в доменной модели осталось на будущее, но как шлюз Dev'а **не используется** (автономная работа).
- Тесты не зелёные → max_turns → FAILED, MR не открываем.
- MR открывается как draft (`DEV_MR_DRAFT=true`).
- RAG по истории MR — отложено на Phase 2.5 (не блокирует базовый цикл).

## Фазы

- ✅ **Фаза 0** — скелет, Jira polling, task list в дашборде.
- ✅ **Фаза 1** — Analyst + Researcher + Communicator (read-only). Планы через Claude Agent SDK, Jira-комменты, injection-фильтр, durable message bus.
- ✅ **Фаза 2** — первый Dev-агент (bellingshausen backend) на отобранных "чистых" тикетах. GitLab VCS, workspace isolation, bot identity на коммитах, draft MR.
- ✅ **Фаза 2.5** — RAG по истории MR. `EmbedderPort`+`FastembedEmbedder` (ONNX без torch) + `MrHistoryPort`+`LocalMrHistory` (SQLite blob + numpy cosine). Модель: `paraphrase-multilingual-MiniLM-L12-v2` (384 dim, ~220MB). Новая таблица `mr_history`, новый тул Researcher'а `search_mr_history` доступен Analyst'у и Dev'у. CLI: `virtual-dev index-mrs --repo <key>`.
- ✅ **Фаза 3 — СДЕЛАНО (2026-04-24)** — Reviewer + DevOps + write-side Communicator. Теперь бот не только открывает MR, но и ведёт его до мержа: поллит комменты, считает апрувы, пингует ревьюеров, эскалирует тимлиду при простое, замечает красный CI и сигналит в MM.
  - **Mattermost write-side** (`adapters/chat/mattermost.py`): `send_direct` (через `create_direct_message_channel`) и `send_to_channel`. WebSocket `subscribe` пока `NotImplementedError` — Phase 3 работает на polling'е, MM-входящие не нужны.
  - **Communicator расширен** (`application/services/communicator.py`): `send_dm`, `send_channel` (с rate-limit: sliding window per target по `rate_limit_per_hour` из конфига) + working-hours gate (`WorkingHoursCfg`, tz Europe/Moscow, 10–20 пн–пт; отключается `COMMUNICATOR_RESPECT_WORKING_HOURS=false` в env). `resolve_user_id(username|email)` — единая точка lookup'a. Новый DTO `SendOutcome(sent, skip_reason)` — агенты видят, почему сообщение не ушло.
  - **VcsPort расширен**: `get_mr_approvals` → `ApprovalInfo(approved_by, required, count)`, `get_latest_pipeline_jobs` → `list[PipelineJob]` с tail'ом лога для failing-job'ов. В `GitLabVcs` — реализация через `mr.approvals.get()` и `project.pipelines.get(id).jobs.list(all=True)` + `job.trace()` для N последних строк лога.
  - **`ReviewerAgent`** (`application/agents/reviewer.py`): на каждый tick проходит по открытым MR из БД, diff'ит комменты против `last_seen_comment_id`, классифицирует эвристиками (`classify_comment` → approval_hint / question / change_request / chatter). Новые human-комменты релеит в MM (канал `mappings.team_channels[repo_key]` или DM `escalation.mattermost_user`). При достижении порога апрувов (`review_policy.required_approvals`) — публикует `mr.approved` + пинг "please merge". Escalation policy: `ping_reviewers_after_hours` → один пинг в канал, `escalate_after_hours` → DM тимлиду. Учёт состояния в `MergeRequestRow.{last_seen_comment_id, last_activity_at, ping_reviewers_at, last_escalation_at}`.
  - **`DevOpsAgent`** (`application/agents/devops.py`): опрашивает `get_latest_pipeline_jobs` для всех открытых MR. Detect: transition "any-state → failed" — постит в MM summary с failing-job логами. Idempotent: `last_pipeline_notified_status="failed"` блокирует повторное уведомление; recovery (→ success) очищает флаг. Авто-фикса нет (осознанно — оставлено на будущее).
  - **`PollerWorker`** (`runtime/workers/poller.py`): простая "каждые N секунд вызови список tick-callables" обёртка; мы запускаем под ней `reviewer.tick` и `devops.tick` из web lifespan. Интервалы в `.env`: `REVIEW_POLL_INTERVAL_SECONDS=180`, `PIPELINE_POLL_INTERVAL_SECONDS=120`.
  - **Новые topic'и на шине** (`application/agents/orchestrator.py`): `mr.comment`, `mr.approved`, `mr.stuck`, `pipeline.failed`. Использование — информационное (события для будущего Reviewer-LLM-inbox), шина не блокирующая.
  - **Новые колонки MR**: `last_seen_comment_id`, `last_activity_at`, `last_pipeline_notified_status`, `last_escalation_at`, `ping_reviewers_at`. Старую БД пришлось пересоздать: `rm data/virtual_dev.db && virtual-dev db init`.
  - **Container / web app**: `Container.reviewer / .devops`, в web lifespan поднимаются 2 дополнительные `PollerWorker`-таски, `/healthz` показывает их статусы + статы.
  - **CLI**: `virtual-dev review-mrs` и `virtual-dev watch-ci` — one-shot тики для smoke-теста.
  - **Тесты**: 91 unit (было 72). Новое — 19: `test_communicator_write` (6), `test_reviewer` (9), `test_devops` (4). Фейки SDK / GitLab API не поднимаются — тесты дергают `.tick()` со стабом `VcsPort` и recording-Chat'ом.
  - **Побочный фикс**: убрали eager `from .container import ...` в `infrastructure/__init__.py`, иначе `application.services` → `container` → `application.services` циркуляр бьёт при `from virtual_dev.application.agents import ...`. Теперь тесты / CLI импортируют из `virtual_dev.infrastructure.container` явно.

- ✅ **Phase 2 smoke-test стабилизация (2026-04-24)** — первый успешный end-to-end прогон `DM-3287 → MR !1000 в bellingshausen`. Разобрали пачку проблем:
  1. `plan-task` / `dev-task` падали, если задача не в БД — добавили `_ensure_task_in_db` (auto-fetch из Jira).
  2. Jira 401 — переключили с Basic на Bearer (PAT).
  3. `claude` CLI exit code 1 при превышении `max_turns` в `tool_use` → SDK бросает generic Exception без stderr. Починили в адаптере `ClaudeAgentSdkCodeAgent`: capture stderr через callback + если ResultMessage уже получен — swallow exit-1 как soft-timeout (stop_reason=max_turns).
  4. Бампнули лимиты: `analyst` 15→40, `developer` 30→80. 15 turns не хватало даже на разведку.
  5. Прогресс-лог каждого `tool_use` в Claude-адаптере — видно чем занята модель.
  6. VCS уважает `local_path` (раньше всегда клонил в workspaces), но с safety-check на dirty tree **один раз на входе**.
  7. Double ticket prefix в MR title / commit msg (`Draft: DM-3287: [DM-3287] ...`) — `_strip_ticket_prefix` снимает ведущий `[KEY]`/`KEY:` с модели + инструкция в system prompt "не ставь префикс".
  8. MR URL не логировался нигде → добавили `logger.info("opened MR !{iid}: {url}")` в `dev.py`.
  9. Правила по комментариям ("зачем, а не что") — в `_DEV_SYSTEM_BASE` + `config/rules/dev-bellingshausen-{backend,frontend}.md`.
- ✅ **Фаза 3** — Reviewer + DevOps + запись в Mattermost. Полный цикл: бот пингует ревьюеров, классифицирует комменты, собирает апрувы, замечает красный CI, эскалирует тимлиду при простое. Мерж — ручной (человек).
- ✅ **Phase 3.x polishing (2026-04-25)** — обкатка end-to-end на реальных DM-задачах вскрыла серию мелких/средних проблем; все починены, поведение и конфиг устаканены.
  - **Jira transitions** — `set_issue_status` библиотеки давала невнятный `'transition' identifier must be an integer` если у workflow нет transition'а с целью равной нашему имени. Адаптер теперь сам ищет `to == <status>` (case-insensitive); если нет — поднимает понятную ошибку со списком доступных. В DM-проекте статусы — `In Review` (не "Review"), `Closed` (не "Done"); конфиг поправлен.
  - **MR title / draft** — `GitLabVcs.create_merge_request` ставил `Draft: ` префикс прямо в title. Снятие draft через UI не убирало текст → `Draft: DM-... :` лез в MM-пинг. Перевели на dedicated `draft: true` API-флаг.
  - **MM WebSocket** — у нас была пачка проблем подряд: (а) забыли вызвать `driver.login()` перед чтением `client.token` → отправляли auth-challenge с **пустым** токеном, MM закрывал соединение (`no close frame received or sent` / 1006); (б) order SSL-флагов на Python ≥3.12: `verify_mode=CERT_NONE` нельзя ставить когда `check_hostname=True`, надо сначала `check_hostname=False`; (в) бесконечный 3s-retry упирался в WAF rate-limit, `timed out during opening handshake`. Сделали exponential backoff (cap 5min) + правильный SSL-порядок + eager login в subscribe(). Реальный loopback (отправили пост → получили обратно по WS) подтверждает работу.
  - **MM URL** — корректный домен `mm.2gis.one` (не `mattermost.2gis.ru`); SSL за self-signed — `MATTERMOST_SSL_VERIFY=false` в `.env`. Адаптер теперь принимает `ssl_verify` + `ssl_ca_file`.
  - **GitLab MR notes order** — по умолчанию `notes.list()` отдаёт newest-first, а Reviewer'ский `_new_comments` шёл по списку oldest→newest для cutoff'а через `last_seen_comment_id`. Из-за порядка новые комменты пропускались. Добавили `order_by=created_at, sort=asc` явно.
  - **`git push` retry** — GitLab иногда отвечает `Internal API unreachable` / connection reset. Push с локальным retry до 3 раз с linear backoff на эти transient-маркеры (плюс HTTP 5xx).
  - **Local config / git история** — все персональные значения (channel id, MM handle, абсолютный `local_path`) перенесены в `config/local.yaml` (gitignored). Loader научился `repositories_patch` (точечный patch одной репы по key, а не replace всего списка). Из git-истории отдельно вычистили коммиты, в которых `.env.example` содержал реальные токены (через `git filter-repo --replace-text`).
  - **Bot identity для Reviewer-фильтра** — раньше я фильтровал свои GitLab-комменты по `MATTERMOST_BOT_USERNAME`, что неверно (это ММ, не GitLab). Стал фильтровать по `row.author_username` (автор MR — это и есть наш бот). Сейчас explicit `bot_username` в Reviewer = None → ничего не фильтруем (single-user setup).

- ✅ **Phase 3.5.5 (2026-04-25)** — выноска **всех** шаблонов сообщений в конфиг + LLM-промптов в файлы; pipeline-aware review ping; auto-fix CI без шума в каналы.
  - **Шаблоны MM/Jira/MR в `config/notifications.yaml`** — секции `mattermost:` (review/merge/stale/escalation_dm/thread_reply_*), `jira:` (plan_comment, mr_link_comment, failure_comment), `merge_request:` (title, commit_message, description). Загружается отдельным yaml через loader, можно override'ить в `local.yaml` через `notifications_override`. Все f-string'и ушли — рендеры в коде только готовят `*_block` строки (для conditional-секций) и подставляют через `str.format`.
  - **LLM системные промпты в `config/prompts/{analyst,dev,thread_responder}.md`** — markdown-файлы с placeholder `{untrusted_warning}` (туда подставляется `SYSTEM_PROMPT_ABOUT_UNTRUSTED` из injection-фильтра). Новый `PromptsLoader` (паттерн `RulesLoader`) с кешем per-process. Все три агента принимают `prompts_loader` как required kwarg. **Системные промпты НЕ в notifications.yaml** — они принципиально другие: это часть LLM-логики, не «бот говорит человеку».
  - **Reviewer держит review_ping пока CI не зелёный** — перед отправкой "please review" смотрим `live.pipeline_status`. Если FAILED / PENDING / RUNNING → пингования нет (логируем "holding ping"); SUCCESS / UNKNOWN → пингуем. Это deterministic gate; параллельно в `dev.md` промпте явное "CI must be green before submit_mr".
  - **DevOps авто-фикс CI** — кардинальная смена поведения. Раньше: красный CI → "Pipeline FAILED" в #dm-test. Теперь: красный CI → бот САМ пытается починить. На каждом тике (если CI red): берём **полный** лог упавших job'ов, файрим `Dev.handle_iteration` (background task, не блочит поллер), считаем попытки. После `max_autofix_attempts=3` неуспехов → DM `escalation.mattermost_user`. **Канал ВООБЩЕ не видит CI-failures** — это не его дело. Канал видит MR ровно один раз: "ready for review" (когда CI зелёный). Новые колонки `MergeRequestRow.{pipeline_autofix_attempts, pipeline_autofix_escalated}`. Конфиг `pipeline_policy.max_autofix_attempts` в agents.yaml. На зелёном — счётчик/флаг сбрасываются.
  - **DevAgent.handle_iteration** — был добавлен в Phase 3.5 для thread-driven правок; теперь второй потребитель — DevOps. `VcsPort.checkout_existing_branch` (fetch + `checkout -B branch origin/branch`) делает state-clean checkout без потери чужих коммитов. DevAgent + dev_by_repo dict вынесены в `Container` (раньше строились в web lifespan), чтоб MmThreadListener и DevOps делили одни инстансы.
  - **Тесты**: 100 (было 91). Полный rewrite `test_devops.py` под auto-fix (3 нативных теста + collapse_status). Reviewer тесты получили 2 новых для pipeline gate.

- ✅ **Фаза 3.6 (2026-04-25 вечер)** — итеративные правки + auto-fix CI + GitLab-комменты, плюс пакет багфиксов после прогонов на реальных DM-задачах.
  - **Silent iteration push** — бот больше не объявляет "Внёс правку" сразу после iteration-push'а. Push идёт **молча**; на следующем тике Reviewer видит CI зелёный для нового коммита и постит **"✅ Внёс правки, CI зелёный — коммит abc... на ai-dev/..."** в тот канал откуда пришёл фидбек (MM-тред ИЛИ top-level GitLab MR коммент). Новые колонки `MergeRequestRow.iteration_pending_ci_sha` + `iteration_ack_target` (`'mm'`/`'gitlab'`).
  - **Auto-fix на красном CI** в DevOps — без шума в каналы команды. На красном пайплайне DevOps вызывает `Dev.handle_iteration` с **полным** логом упавших job'ов; до этого был баг — `log_tail_lines=0` интерпретировался как "не качай лог" (`if > 0`), Dev получал название job'а без traceback'а и решал не ту проблему. Семантика: `>0` tail, `<0` full, `0` skip. После `pipeline_policy.max_autofix_attempts` (3) — DM `escalation.mattermost_user`. Счётчик сбрасывается на зелёном CI и при iteration из MM-треда (свежий запрос → свежий бюджет).
  - **Reviewer гейт** на review_ping использует `get_latest_pipeline_jobs` + `_collapse_status`, а не `mr.pipeline.status` (которая брифли desync'ится после push'а — `mr.pipeline` либо None, либо ещё указывает на старый запуск). `created`/`manual`/`skipped` job-статусы считаются "passing" (это обычно downstream deploy-гейты, не CI на код).
  - **GitLab actionable комменты** (`question` / `change_request`) Reviewer прокидывает в тот же `ThreadResponderAgent` что и MM-тредные. Decision:
    - `reply` → `VcsPort.add_mr_comment` (top-level MR note; threaded reply на конкретный discussion — Phase 5 nice-to-have).
    - `iterate` → `Dev.handle_iteration(feedback=коммент)`, на iteration push'е ставится `ack_target='gitlab'` → ack тоже постится в GitLab.
    - `ignore` → молчание.
  - **System notes фильтр** — GitLab `added 1 commit` / `left review comments` / draft toggles имеют `system: True`. `_new_comments` их фильтрует, не маршрутизирует через LLM.
  - **`VcsPort.add_mr_comment`** + реализация в GitLabVcs (`mr.notes.create`). `ReviewComment.system` добавлено.
  - **Defensive `commit_all`** — если LLM игнорирует "Do NOT commit" и сам зовёт `git commit` (модель такая бывает), наш адаптер сравнивает local HEAD vs `origin/<branch>` и всё равно пушит коммит — с WARNING про неправильного автора. Без фикса коммит "терялся": `git status --porcelain` пустой → возвращали `""` → handle_iteration говорил "правка не потребовалась".
  - **Draft через title-префикс** — `draft: true` API-поле self-hosted GitLab silently дропает. Вернулся к универсальному `Draft: ` префиксу в title; GitLab стрипает его при "Mark as ready" автоматически.
  - **MR notes oldest-first** — `mr.notes.list(order_by=created_at, sort=asc)` явно. Default desc ломал cutoff-логику Reviewer'а.
  - **LLM system prompts** в `config/prompts/{analyst,dev,thread_responder}.md` через `PromptsLoader`. Markdown-файлы с placeholder'ом `{untrusted_warning}` для injection-фильтра. Edit → restart → меняется поведение.
  - **Все bot-authored шаблоны** Mattermost / Jira / MR-meta в `config/notifications.yaml`. `str.format` с готовыми `*_block` строками (conditional-секции вычисляются в коде, YAML остаётся плоским).
  - **CI auto-fix gives Dev FULL log** — раньше gate `if log_tail_lines > 0` рубил тру-фулл вариант. Теперь negative tail_lines = full log.
  - Тесты: 102 (на старте сегодняшней сессии было 91). Новые: pipeline-gate review_ping, autofix dispatch+escalation, iteration silent-push state, listener+settings, GitLab comment routing.

- ✅ **Фаза 3.5** — MM-тред как канал ревью. Бот слушает WebSocket (с SSL-фиксом от poker-planning-bot), при каждом реплае в "please review"-треде спрашивает у `ThreadResponderAgent` (LLM через claude-agent-sdk): ответить текстом / внести правку / молча проигнорировать. Агент знает про injection-фильтр, может послать коллег "погуляй" если фидбек бредовый. Для правок: `DevAgent.handle_iteration` — checkout существующей ветки (`VcsPort.checkout_existing_branch`), новый коммит поверх, push. GitLab автоматически обновляет MR. Идемпотентность через реакцию ✅ (`white_check_mark`) на обработанном посте. Новые колонки `MergeRequestRow.review_thread_{channel_id,root_id}`. Новый worker `MmThreadListener` в web lifespan. 97 unit-тестов.

- ✅ **Phase 3.8 (2026-04-26)** — переписан clarification flow в нормальную доменную модель.
  Старый `ClarifierService` (один DM = один ответ, всё первое сообщение в DM) удалён, заменён на дерево вопросов с LLM-классификацией ответов.
  - **Доменные модели** (`src/virtual_dev/domain/models/clarification.py`):
    - `Question` — узел дерева. `parent_id`/`root_id`/`chain_depth` задают редирект-цепочку.
    - `Stakeholder` (`StakeholderKind`: EXPLICIT_HANDLE / EMAIL / TASK_AUTHOR / TEAM_CHANNEL / TEAM_LEAD / UNRESOLVED_NAME / BOT) — кому задаём вопрос.
    - `Answer` — фрагменты + coalesced_text + классификация + extracted-payload.
    - `QuestionState`: PENDING → ASKING → COALESCING → CLASSIFYING → {ANSWERED | REDIRECTED | COUNTER_PENDING | ASKING_FOR_STAKEHOLDER | ABANDONED | ESCALATED}.
    - `Classification`: DIRECT / REDIRECT / COUNTER_QUESTION / DONT_KNOW / OUT_OF_SCOPE / HANDLE_PROVIDED.
    - `CounterQuestionKind`: FACTUAL (бот сам отвечает) / BUSINESS (эскалация автору тикета).
  - **БД (`infrastructure/db/models.py`)**: дропнули `clarifications`, добавили три таблицы:
    - `questions` — узлы дерева; индексы `(state, last_fragment_at)` для горячих query coalescer'а, `deadline_at` для sweep'а.
    - `question_fragments` — сырые MM-сообщения, `mm_post_id UNIQUE` для идемпотентности WS-replays.
    - `question_answers` — итог классификации (1:1 с question, опционально), audit trail с `extracted_json`.
  - **Компоненты:**
    - `QuestionRepository` (`application/services/clarification/repo.py`) — единственное место, что трогает rows. `chain_user_ids()` для cycle-detection.
    - `AnswerClassifier` (`application/agents/answer_classifier.py`) — Haiku 4.5, mirror'ит ThreadResponderAgent: structured submit_classification через `@tool`+JSON-schema. Промпт `config/prompts/answer_classifier.md` детализирует все 6 типов ответов и subkind для counter-Q.
    - `CounterQuestionAnswerer` (`application/agents/counter_answerer.py`) — Sonnet 4.5 с Read/Glob/Grep + Researcher. Output `{answer_text, confidence, escalate_to_reporter}`. По умолчанию `counter_question_confidence_threshold=0.6` — ниже — fallback на BUSINESS-путь.
    - `StakeholderResolver` (`application/services/clarification/stakeholder_resolver.py`) — детерминистика для `@nick`/email; для свободно-формного имени → LLM (`stakeholder_resolver.md`) пытается дать `firstname.lastname` транслитерацию. Если LLM confidence ниже 0.8 ИЛИ MM не находит — `UNRESOLVED_NAME` → orchestrator спавнит ASKING_FOR_STAKEHOLDER.
    - `ClarificationOrchestrator` (`application/services/clarification/orchestrator.py`) — owner state machine'а, `apply_classification` маршрутизирует все 6 классификаций, плюс `flush_idle()` (coalescer-tick) и `sweep_deadlines()` (timeout-tick).
  - **Coalescing**: люди отвечают порциями («дай минуту… так… в общем смотри в коде у Васи… ой нет, лучше у Пети»). `MmThreadListener` теперь НЕ классифицирует на каждом event'е — только append'ит fragment + reset'ит idle-timer. `AnswerCoalescerWorker` (PollerWorker-обёртка с двумя tick'ами `flush_idle`/`deadline_sweep`) раз в 60s проверяет: если `last_fragment_at + coalesce_window_seconds (default=600) <= now` → coalesce all unflushed fragments → call AnswerClassifier → drive state machine. Пока человек пишет — мы молчим. Mid-message ack удалён (читался как «бот перебил»).
  - **Loop guards** (все pure-state, без LLM):
    - `max_chain_depth=4`: a→b→c→d → пятый редирект → ABANDONED + ESCALATED.
    - **Cycle detection**: `chain_user_ids` от родителя → если редирект резолвится в уже-в-цепочке user_id → escalate.
    - `max_question_age_hours=48`: deadline_sweep tick'ом.
    - `max_subquestions_per_root=10`: страховка от runaway-дерева.
  - **Counter-question hybrid mode** (по выбору user'а):
    - FACTUAL counter-Q (типа «какая из 10 ручек?») → `CounterQuestionAnswerer` сам читает Issue + код через Read/Glob/Grep + Researcher → постит контекст в DM-тред respondent'а → родитель остаётся в ASKING (idle-timer крутится для следующего ответа).
    - BUSINESS counter-Q (типа «что важнее — скорость или точность?») → spawn child Question со stakeholder=task.reporter_id. Standard clarification cycle.
    - Низкий confidence FACTUAL → fallback на BUSINESS-путь автоматически.
  - **Re-publish task.discovered**: когда все root-Q дерева в terminal-state и хотя бы один в ANSWERED — orchestrator складывает Q&A-блок в `task.description`, помечает план `superseded`, публикует `task.discovered` → Analyst переплáнирует. Цикл может повториться (новый план снова clarifying — по новой задаём вопросы).
  - **Конфиг (`config/agents.yaml`)**: новая секция `clarification:` с tunable'ами; новые агенты `answer_classifier`/`counter_answerer`/`stakeholder_resolver` в блоке `agents:` (модели — lightweight/default/lightweight соответственно). `config/notifications.yaml`: 6 новых шаблонов (`clarifier_redirect_ack`, `clarifier_handle_request`, `clarifier_counter_factual_intro`, `clarifier_out_of_scope_ack`, `clarifier_dont_know_ack`, `clarifier_escalation_to_lead`). `config/prompts/{answer_classifier,counter_answerer,stakeholder_resolver}.md`.
  - **CLI**: `virtual-dev clarifications show DM-XXXX` — печатает дерево вопросов со state/stakeholder/answer (rich.tree).
  - **Дашборд**: `/tasks/{id}` рендерит дерево вопросов с indent по `chain_depth`, видны state каждого узла + extracted-классификация ответа.
  - **MM Listener**: переписан с `accept_answer` (сразу-классифицируем) на `append_fragment` (буферизуем, ждём coalescer'а). Lookup по thread + fallback по channel+author (FIFO oldest active) сохранён.
  - **Тесты: 137** (было 108): −6 удалённых из старого test_clarifier, +35 новых:
    - `test_clarification_repo.py` (7) — round-trip + idempotent fragment + cycle-detection + idle/overdue queries.
    - `test_clarification_orchestrator.py` (10) — все state-transitions + max_chain_depth guard + DONT_KNOW + counter FACTUAL/BUSINESS + UNRESOLVED_NAME → handle_request + deadline_sweep + OUT_OF_SCOPE.
    - `test_answer_classifier.py` (6) — все 6 классификаций + malformed payload + invalid string fallback.
    - `test_stakeholder_resolver.py` (6) — explicit/email/free-form/low-confidence/give-up/MM-not-found.
    - `test_counter_answerer.py` (4) — high/low confidence + no-capture + clamp.
    - `test_mm_thread_listener_clarification.py` (2) — fragment-append (threaded + plain DM), no mid-message ack.
  - **Schema migration**: `rm data/virtual_dev.db && uv run virtual-dev db init` (или просто `db init` — `create_all` идемпотентен и добавит новые таблицы; старая `clarifications` останется-неиспользуемой).

- ✅ **Фаза 3.7 (2026-04-25 поздний вечер)** — техдолг + clarification flow. Закрыли 9 пунктов техдолга и реализовали критический функционал «бот сам уточняет инфу у людей до того как пускать Dev в код».
  - **Clarification flow** (`application/services/clarifier.py` — новый сервис):
    - Аналитик может в `submit_plan` поставить `status: clarifying` + `open_questions[]`. Промпт `analyst.md` обновлён: явно требует ставить clarifying когда в тикете "уточнить у X / спросить Y / API будет позже / схема TBD" — было бы строго лучше задать лишний вопрос, чем зашиппить неправильный код.
    - При CLARIFYING-плане `AnalystInbox` зовёт `ClarifierService.request_clarifications(...)`: каждый вопрос идёт DM'ом в Маттермост к `ask_whom` (ресолвится как username, если не получилось — как email; иначе — fallback на `escalation.mattermost_user` с пометкой "перенаправлено"). Каждый отправленный DM-пост записывается строкой в новой таблице `clarifications` (см. `ClarificationRow` ORM).
    - Когда человек отвечает в DM, `MmThreadListener._dispatch` сначала смотрит, не реплай ли это под нашим вопросом-DM'ом (по `mm_root_post_id` для tread-ответа или по `mm_channel_id`+автору для plain-DM ответа). Если да — `ClarifierService.accept_answer(...)` записывает ответ; ack-постится в DM ("спасибо, записал"). Идемпотентность через ✅-реакцию на пост-ответ.
    - Когда последний вопрос для плана отвечен, Clarifier:
       - дописывает Q&A-блок в `task.description`,
       - старый план помечает `status=superseded`,
       - публикует на шине новый `task.discovered` → Analyst переплáнирует уже с уточнениями. Цикл может повториться, если новый план снова clarifying.
    - В дашборде на `/tasks/{id}` появилась секция "Уточнения" со статусом каждого вопроса (ждём/отвечено) + история Q&A.
    - Конфиг: `notifications.mattermost.{clarifier_question, clarifier_answer_ack, clarifier_all_answered_ack}` — вынесены в `config/notifications.yaml`. Дефолты на русском.
    - Тесты: `test_clarifier.py` — 6 кейсов (DM-диспатч с разными формами ask_whom, идемпотентность, fallback на тимлида, цикл "ответ → re-publish → supersede", FIFO-pickup для plain-DM ответа).
  - **#13 Bot self-comment loop в GitLab** — Container'е резолвим свой GitLab username через `gl.user.username`, прокидываем в `ReviewerAgent.bot_username`. Теперь `_is_bot_author` явно дропает комменты от нашего MR-автора (а не только по `row.author_username`).
  - **#2 Hot-reload системных промптов** — `PromptsLoader` теперь кеширует по `(name, mtime_ns)`. Edit `config/prompts/*.md` → следующий tick подхватывает без рестарта. Лог `reloaded prompt {name} (file changed)` подтверждает hit.
  - **#11 Concurrent task workspace race** — `GitLabVcs._repo_locks: dict[str, asyncio.Lock]`. Все мутирующие локальные ops (`fetch_and_checkout`, `checkout_existing_branch`, `create_branch`, `commit_all`, `push`, новый `merge_base_into_current`) под per-repo lock'ом. `ensure_clone` без lock'а — идемпотентен и читать race здесь не страшно.
  - **#12 Merge-conflict on iteration** — новый `VcsPort.merge_base_into_current(repo, base) -> bool` (default-impl `True`, реализация в GitLabVcs делает `git merge --no-edit origin/<base>`, на conflict — `git merge --abort` + `False`). `Dev.handle_iteration` после `checkout_existing_branch` зовёт его; на False возвращает `DevResult(FAILED, stopped_reason=merge-conflict-with-<base>)` — человек разруливает руками.
  - **#1 GitLab reply threading** — `ReviewComment.discussion_id` добавлено в доменную модель. `GitLabVcs.list_review_comments` теперь идёт через `mr.discussions.list(...)`, плюский notes-список собирается с привязкой к discussion. `reply_to_comment` использует discussion id (с graceful fallback на top-level note). `Reviewer._handle_gitlab_actionable` для REPLY decision'а вызывает `reply_to_comment` если знает discussion_id, иначе `add_mr_comment`.
  - **#14 ThreadResponder MR diff** — `VcsPort.get_mr_diff(repo, iid) -> str` (default `""`, в GitLabVcs синтезирует unified-diff из `mr.changes()` с обрезанием на ~50KB). `ThreadResponderAgent.decide(..., mr_diff=...)` принимает диф и встраивает в prompt блоком ```diff. И `MmThreadListener`, и `Reviewer` теперь подтягивают diff и пробрасывают.
  - **#4 Rate-limit handling** — `ClaudeAgentSdkCodeAgent` обернут в retry-loop с экспоненциальным backoff'ом. Detector — regex по тексту exception/stderr (`rate_limit | 429 | too many requests | 5h limit | usage limit reached`). По дефолту 2 попытки c 60s/180s sleep'ом. Если падение — не rate-limit, поднимаем сразу.
  - **#9 Dashboard улучшения (partial)** — `/mrs` показывает столбцы `pipeline_status` (с CSS-классом), autofix-attempts (с warning-иконкой если escalated), pending CI sha. На `/tasks/{id}` MR-список тоже расширен. Появилась секция "Уточнения" (см. clarification flow выше). Полный rewrite дашборда (timeline, override-кнопки) — отложено.
  - **Тесты: 108** (было 102): добавлены 6 в `test_clarifier.py`. Существующие 102 не сломались.

- **Phase 3.7 — отложено в техдолге (зафиксировано здесь, делаем позже):**
  - **#5 Vault для секретов** — пока `.env` достаточно. Vault интеграция требует выяснить, какой Vault в компании, согласовать политику. Не блокер для реальных задач.
  - **#6 Long-running stability** — нужны метрики память/CPU/connections + автоматический recycle WS подключения, перезапуск после OOM. Делается, когда боль реально возникнет на продакшен-нагрузке.
  - **#7 Monitoring** — Prometheus/Grafana, alerts. Сейчас лога loguru хватает. Нужно при росте репо/команды.
  - **#8 Test coverage** — 108 unit-тестов покрывают happy-path. Нужны e2e тесты с реальным GitLab/Jira/MM в docker-compose. Дорого собирать, делается, когда стабилизируется фичеспек.
  - **#10 Web dashboard полный rewrite** — таймлайн событий, кнопки override "пни ревьюеров вручную", "перепланируй", "stop". Сейчас сделана только partial-секция. Когда будет команда пользоваться дашбордом регулярно.
  - **Alembic migrations** — пока БД пересоздаётся через `db init` при изменениях схемы (теряем данные). При работе на проде нужны нормальные миграции; пока на dev'е допустимо.

- **Фаза 4** — обкатка на реальных задачах всей команды.
- **Фаза 5** — автопилот, все репо, фронт-агенты, auto-fix CI DevOps-агентом, LLM-классификация комментов (замена эвристик из Phase 3).

## Стиль работы
- Пиши по-русски в ответах, в коде — английский (docstrings тоже английский, но допустимы русские комментарии для бизнес-контекста).
- Я (пользователь) — тимлид, знаю Python хорошо. Можно без "для чайников".
- Предпочитаю обсудить архитектурные решения перед тем как писать много кода.
- Fail loud, не глотай ошибки молча.
- Типизация строгая (mypy strict), pydantic для DTO, dataclasses для domain.