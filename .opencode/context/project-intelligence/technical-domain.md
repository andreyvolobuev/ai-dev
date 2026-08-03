<!-- Context: project-intelligence/technical | Priority: high | Version: 1.2 | Updated: 2026-07-06 -->

# Technical Domain

## Primary Stack
| Layer | Technology | Rationale |
|-------|-----------|-----------|
| Language | Python 3.13+ (>=3.13) | Совместимость зависимостей |
| Package Manager | uv | Быстрее pip, единый формат |
| Agent Framework | Claude Agent SDK (`claude-agent-sdk` на PyPI) | Обёртка над `claude` CLI, через залогиненную Claude Max сессию |
| LLM | Claude Opus 4.8 (`claude-opus-4-8`), Haiku 4.5 (`claude-haiku-4-5-20251001`) | Max подписка, датированные ID для корпоративного прокси |
| Task Tracker | Jira (`jira.2gis.ru`, self-hosted) | `atlassian-python-api`, PAT (Bearer) |
| VCS | GitLab (`gitlab.2gis.ru`, self-hosted) | `python-gitlab`, PAT |
| Chat | Mattermost (`mm.2gis.one`, self-hosted) | REST API + WebSocket, self-signed SSL |
| KB | Confluence (`confluence.2gis.ru`, self-hosted) | REST API, CQL search |
| DB | **PostgreSQL** (`postgresql+asyncpg`) | asyncpg + psycopg2-binary (для Alembic). Раньше был SQLite |
| Dashboard | FastAPI + Jinja2 | Web UI |
| CLI | typer | CLI-команды |
| Deploy | Docker + Kubernetes (Helm) | Корпоративный registry `docker-hub.2gis.ru` |

## CRITICAL: LLM-инфра — два режима работы

### Режим 1: Claude Max (локальная разработка)
- `claude-agent-sdk` → subprocess `claude` CLI → залогиненная Claude Max сессия
- **API-ключ НЕ нужен**. Пакет `anthropic` в зависимостях НЕ нужен.
- `ANTHROPIC_API_KEY` нигде не ставится
- Нет budget-лимитов: у Max нет per-token биллинга. Не добавлять `PER_TASK_BUDGET_USD`, `max_tokens_per_turn`, `max_budget_usd`, `temperature`
- Единственный лимит — `max_iterations_per_task` (aka `max_turns`): защита от runaway-циклов
- `plans.cost_usd` в БД — оценочная цифра, только для аналитики
- Rate-limit: 5-часовое окно. SDK выдаёт `RateLimitEvent`. Backoff через retry-loop (2 попытки, 60s/180s sleep)

### Режим 2: Корпоративный LLM-шлюз (продакшн / K8s)
- **URL**: `https://ai-openai-proxy.k8s.n3.2gis.io/anthropic` (Anthropic-compatible proxy)
- **Auth**: `ANTHROPIC_API_KEY` → уходит как `x-api-key` header (НЕ Bearer/`ANTHROPIC_AUTH_TOKEN` — даёт 401)
- **Включение**: задать `ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY` в `.env` (или K8s Secret)
- `_build_claude_env()` в `container.py` прокидывает их в `ClaudeAgentOptions.env` + ставит `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`
- **Строгий шлюз**: режет beta-поля Claude Code (например `context_management`) с `400 "Extra inputs are not permitted"` → `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` обязателен
- **Модели**: только датированные ID из `/anthropic/v1/models`. Короткие алиасы (`claude-sonnet-4-5`, `claude-haiku-4-5`) дают 404
  - Основная: `claude-opus-4-8` (работает везде)
  - Лёгкая: `claude-haiku-4-5-20251001` (датированный)
- Если `ANTHROPIC_BASE_URL` не задан → SDK наследует parent env, использует локальный `claude` login (режим 1)

## Architecture — Hexagonal (Ports & Adapters)
```
domain/         # Модели и интерфейсы (ports). Без внешних зависимостей.
application/    # Агенты, workflows, services. Зависят только от портов.
adapters/       # Реализации портов (Jira, GitLab, Mattermost, Confluence, ...)
infrastructure/ # БД (async SQLAlchemy 2.0 + Alembic), конфиг (pydantic-settings + yaml-loader), DI (Container), loguru
presentation/   # Web-дашборд (FastAPI+Jinja2), CLI (typer), webhooks
runtime/        # Воркеры: PollerWorker, AgentRunner, AnalystInbox, DevInbox, MmThreadListener, AnswerCoalescer
tools/          # MCP tools (19 шт), авто-discovery через _loader.py
```

Смысл: замена адаптера (Mattermost→Slack, Jira→Trello) не трогает domain и application.

## Agents
Каждый — отдельная сессия Claude Agent SDK со своим контекстом.

| Agent | Role | Model | Subscribed to |
|-------|------|-------|---------------|
| **Orchestrator** | Маршрутизация, эскалация, metadata | — | Jira polling → `task.discovered` |
| **Analyst** | Читает тикет + Confluence + MM-треды, строит Plan. Итеративные уточнения через DM | Opus 4.8 | `task.discovered` |
| **Researcher** | RAG: git grep, read file, Confluence search, MR history | — | Запросы от других агентов (in-process MCP) |
| **Communicator** | ЕДИНСТВЕННЫЙ, кто пишет в Mattermost. Injection-фильтр | — | Вызовы из агентов |
| **Dev (N штук)** | По одному на (репо, специализация) | Opus 4.8 | `plan.ready` |
| **Reviewer** | Комменты в MR, апрувы, пинги, эскалация | Haiku 4.5 | Tick-поллинг |
| **DevOps** | CI/CD, красные пайплайны, auto-fix | — | Tick-поллинг |
| **ThreadResponder** | LLM-решение: ответить/внести правку/игнор | Opus 4.8 | Вызовы из Reviewer/MmThreadListener |

**Message Bus**: `SqlAlchemyMessageBus` (durable, PG-backed, dialect-aware upsert). Single-consumer per `to_agent`, `"*"` broadcast.
Topics: `task.discovered`, `plan.ready`, `mr.comment`, `mr.approved`, `mr.stuck`, `pipeline.failed`.

## Services (application/services/)
- **CommunicatorService** — запись в MM (DM, канал, реакции). Rate-limit sliding window. Working-hours gate.
- **InjectionFilter** — `<untrusted_content>`-обёртка с disarmed closing-тегом. Санитайз zero-width/bidi/tag-unicode. 5 классов инъекций.
- **Researcher** — in-process MCP сервер: `search_code` (git grep), `read_file`, `kb_search`, `kb_fetch_page_by_url`, `search_mr_history`.
- **AgentTrace** — structured audit-log событий для дашборда.
- **AnalystSessionRepository** — per-ticket состояние аналиста (awaiting DM, conversation log, fragments, deadlines).
- **PromptsLoader** — hot-reload системных промптов из `config/prompts/*.md` по mtime.
- **RulesLoader** — подгрузка `config/rules/<agent>.md` в system prompt агента.
- **RecoveryService** — восстановление после сбоев (re-publish missed events, sweep stuck tasks).
- **ReviewCommentClassifier** — эвристики: approval_hint / question / change_request / chatter.
- **HealthTracker** — статусы всех адаптеров для /healthz.

## MCP Tools (src/virtual_dev/tools/)
24 файла, из них 19 tool'ов с авто-discovery через `_loader.py`. Каждый tool — модуль с `build(ctx: ToolContext) -> SdkMcpTool | None`. Фильтр по `TOOL_GROUP` (по умолчанию "analyst").

**Доступные тулы:**
- `search_code` — git grep по workspace
- `read_file` — чтение файла из workspace
- `kb_search` — поиск по Confluence
- `fetch_url` — веб-страница → markdown
- `read_pdf_url` / `read_docx_url` / `read_xlsx_url` / `read_image_url` — документы по URL
- `read_jira_ticket` — чтение тикета Jira
- `read_mattermost_thread` — чтение треда MM
- `find_chat_user_by_name` / `lookup_chat_user` — поиск пользователя
- `dm_user` — отправка DM человеку
- `submit_plan` / `submit_mr` / `submit_response` — финальные действия агентов
- `search_mr_history` — RAG по истории MR (Fastembed + cosine)
- `blocked` / `stuck` — сигналы "застрял"
- `_context`, `_helpers`, `_loader`, `_wrap` — инфраструктурные (не тулы)

## Domain Models
```
domain/models/
├── analyst_conversation.py  # ConversationStep, ConversationStepKind (flat log)
├── chat.py                  # ChatMessage, SendOutcome
├── kb.py                    # KBPage, KBPageUrl
├── merge_request.py         # MergeRequest, ApprovalInfo, PipelineJob, ReviewComment
├── mr_history.py            # MrHistoryEntry
├── plan.py                  # Plan, PlanStep, PlanStatus (READY/FAILED)
├── repository.py            # Repository
└── task.py                  # Task, TaskStatus, TaskLink
```

**AnalystConversation** (плоский, не дерево!): append-only лог шагов аналиста. `ConversationStepKind`: PLANNER_DECIDED / BOT_ASKED / HUMAN_REPLIED / NOTE / STALE_FRAGMENT. Coalescing 180s.

## Repositories (7 total)
- `bellingshausen` — монорепа, backend + frontend (единственное активное сейчас)
- `rainbow`, `pts-aggregator`, `greeder` — backend
- `alertilka-backend`, `alertilka-deploy`, `alertilka-ui` — alertilka ecosystem

Добавляются динамически: строчка в `config/repositories.yaml` → перезапуск → появляются Dev-агенты.

## Database

### PostgreSQL (был SQLite, мигрировали)
- **Driver**: `asyncpg` (async), `psycopg2-binary` (sync, для Alembic)
- **DSN**: `DB_DSN=postgresql+asyncpg://sd_bots:qwerty123@host:5432/virtual_dev`
- **Engine**: async SQLAlchemy 2.0, PG pool settings (`pool_size=10, max_overflow=20`)
- **Message Bus**: `SqlAlchemyMessageBus` (был `SqliteMessageBus`) — dialect-aware upsert (`ON CONFLICT` для PG, `INSERT OR IGNORE` для SQLite)
- **Local dev**: `docker-compose.yaml` с PG 16-alpine, порт 5433, volume, healthcheck

### ORM Models + Alembic
9 таблиц в `infrastructure/db/models.py`: TaskRow, MergeRequestRow, AgentMessageRow, BusSubscriptionRow, PlanRow, MrHistoryRow, AnalystConversationStepRow, AnalystConversationFragmentRow, EventRow.

**7 Alembic-миграций** (0001–0007):
- 0001–0006: базовые таблицы + инкрементальные колонки
- 0007: `TIMESTAMP WITHOUT TIME ZONE` → `TIMESTAMPTZ` (все DateTime колонки). На SQLite — no-op.

**Все DateTime колонки — `DateTime(timezone=True)`**. Приложение везде генерирует tz-aware datetime (`datetime.now(timezone.utc)`, Jira отдаёт `+07:00`). SQLite прощал mismatch, asyncpg — нет.

**Миграции запускаются автоматически**: `await container.init_db()` в FastAPI `lifespan` (с 30s timeout guard). Также через `virtual-dev db init`.

## Integration Points
| System | Auth | URL | Notes |
|--------|------|-----|-------|
| **LLM шлюз** | `ANTHROPIC_API_KEY` (x-api-key) | `ai-openai-proxy.k8s.n3.2gis.io/anthropic` | Anthropic-compatible proxy. Только датированные model ID. `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`. Локально — Claude Max без шлюза |
| Jira | PAT (Bearer) | `jira.2gis.ru` | Статусы: `In Review`/`Closed`. Transitions case-insensitive. tz-aware datetimes |
| GitLab | PAT | `gitlab.2gis.ru` | `Draft:` префикс (не `draft: true`). Notes `sort=asc`. Push retry 3x |
| Mattermost | PAT (driver.login) | `mm.2gis.one` | WS + REST. SSL verify=false. Eager login. REST catch-up 60s |
| Confluence | PAT | `confluence.2gis.ru` | CQL search, 3 URL formats for page fetch |

## Docker & Deployment

### Dockerfile
- База: `docker-hub.2gis.ru/devops/library/python-3.13-ubuntu-24.04:0.1.20`
- `uv` вместо poetry, two-layer cache (deps → source)
- Non-root user (`app:app`), `PATH` включает `.venv/bin`
- `CMD ["virtual-dev", "run", "--host", "0.0.0.0"]` — прямой вызов, без `uv run` overhead
- `--host 0.0.0.0` overrides `WEB_HOST` из K8s ConfigMap

### Kubernetes (Helm, отдельный репо `sd-bots-ai-dev-ai-dev`)
- Namespace: `sd-bots-liza`, cluster `master.k8s.2gis.dev:6443`
- Service: port 80 → targetPort 8080
- HTTPRoute → Istio Gateway (canary + stable), host `ai-dev-sd-bots-liza.istio.k8s.2gis.dev`
- `readinessProbe: /healthz` (opt-in через `healthcheckPath`)
- `minReadySeconds: 10`, `helm --atomic --wait --timeout 5m`
- Env: `build-values.py` инжектирует 24 ENV_KEYS + 5 SECRET_KEYS из CI-окружения

## Key Technical Decisions (see also `decisions-log.md`)
- Коммиты: `Virtual Dev <virtual-dev@datamining.2gis.ru>`, per-call `-c user.name/email`
- Ветки: `ai-dev/<external_id>-<slug>`
- Workspace: уважает `local_path` из `repositories.yaml` с safety-check на dirty tree
- Per-repo `asyncio.Lock` для всех мутирующих git-ops
- AnalystConversation: плоский лог, coalescing 180s, circuit breaker `max_planner_calls_per_goal=8`

## Development Environment
```bash
# Setup
uv sync                    # Установка зависимостей
cp .env.example .env       # Настройка секретов
docker compose up -d       # PostgreSQL (если нет внешнего)

# CLI
virtual-dev db init        # Alembic upgrade to head
virtual-dev plan-task DM-1234 [--post]           # Запустить Analyst
virtual-dev dev-task DM-1234 --repo bellingshausen [--post]  # Запустить Dev
virtual-dev run            # Web server + pollers (порт 8080)

# Tests
uv run pytest              # 314 unit tests (SDK/GitLab API не поднимаются)
```

## Related Files
- `business-domain.md` — Business context and team rules
- `decisions-log.md` — Decision rationale for architecture choices
- `living-notes.md` — Tech debt, open questions
