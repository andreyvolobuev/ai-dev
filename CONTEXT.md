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
- **Фаза 4** — обкатка на реальных задачах всей команды.
- **Фаза 5** — автопилот, все репо, фронт-агенты, auto-fix CI DevOps-агентом, LLM-классификация комментов (замена эвристик из Phase 3).

## Стиль работы
- Пиши по-русски в ответах, в коде — английский (docstrings тоже английский, но допустимы русские комментарии для бизнес-контекста).
- Я (пользователь) — тимлид, знаю Python хорошо. Можно без "для чайников".
- Предпочитаю обсудить архитектурные решения перед тем как писать много кода.
- Fail loud, не глотай ошибки молча.
- Типизация строгая (mypy strict), pydantic для DTO, dataclasses для domain.