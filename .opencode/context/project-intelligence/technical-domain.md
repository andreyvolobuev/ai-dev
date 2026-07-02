<!-- Context: project-intelligence/technical | Priority: high | Version: 1.1 | Updated: 2026-07-02 -->

# Technical Domain

## Primary Stack
| Layer | Technology | Rationale |
|-------|-----------|-----------|
| Language | Python 3.13+ (>=3.13) | Совместимость зависимостей, на моей машине 3.14 |
| Package Manager | uv | Быстрее pip, единый формат |
| Agent Framework | Claude Agent SDK (`claude-agent-sdk` на PyPI) | Обёртка над `claude` CLI, через залогиненную Claude Max сессию |
| LLM | Claude Opus 4.8 (основная, `claude-opus-4-8`), Haiku 4.5 (лёгкие, `claude-haiku-4-5-20251001`) | Max подписка |
| Task Tracker | Jira (self-hosted, 2GIS) | `atlassian-python-api`, PAT-аутентификация (Bearer, не Basic) |
| VCS | GitLab (self-hosted, 2GIS) | `python-gitlab`, PAT |
| Chat | Mattermost (self-hosted, 2GIS, `mm.2gis.one`) | REST API + WebSocket, self-signed SSL |
| KB | Confluence (self-hosted, 2GIS) | REST API |
| DB | SQLite (async SQLAlchemy 2.0 + Alembic миграции) | На старте, 6 миграций |
| Dashboard | FastAPI + Jinja2 | Web UI |
| CLI | typer | CLI-команды |

## CRITICAL: LLM-инфра (не путать с Anthropic API!)
- **Нет API-ключа**: бот работает через Claude Max подписку, не через Anthropic API.
- `ANTHROPIC_API_KEY` нигде не ставим, пакет `anthropic` в зависимостях НЕ нужен.
- Вызовы: `claude-agent-sdk` → subprocess `claude` (из PATH) → залогиненный Claude Code.
- **Нет budget-лимитов**: у Max нет per-token биллинга. Не добавлять `PER_TASK_BUDGET_USD`, `max_tokens_per_turn`, `max_budget_usd`, `temperature` и подобное.
- Единственный лимит — `max_iterations_per_task` (aka `max_turns`): защита от runaway-циклов.
- `plans.cost_usd` в БД — оценочная цифра из `ResultMessage.total_cost_usd`, только для аналитики. Ничего не enforce'ится, наружу не показывается.
- Rate-limit: 5-часовое окно на количество сообщений. SDK выдаёт `RateLimitEvent`. Backoff через retry-loop (2 попытки, 60s/180s sleep, regex-detector: `rate_limit|429|too many requests|5h limit|usage limit reached`).
- **Важно**: при работе через корпоративный шлюз (`ANTHROPIC_BASE_URL` в `.env`) принимаются только датированные model ID из `/anthropic/v1/models`. Короткие алиасы (`claude-sonnet-4-5`) дают 404. Поэтому в `config/agents.yaml` используем `claude-opus-4-8` (работает везде) и `claude-haiku-4-5-20251001` (датированный).

## Architecture — Hexagonal (Ports & Adapters)
```
domain/         # Модели и интерфейсы (ports). Без внешних зависимостей.
application/    # Агенты, workflows, services. Зависят только от портов.
adapters/       # Реализации портов (Jira, GitLab, Mattermost, Confluence, ...)
infrastructure/ # БД (SQLAlchemy + Alembic), конфиг (pydantic-settings + yaml-loader), DI (Container), loguru
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
| **Analyst** | Читает тикет + Confluence + MM-треды, строит Plan. Итеративные уточнения через DM | Opus 4.8 (default) | `task.discovered` |
| **Researcher** | RAG: git grep, read file, Confluence search, MR history | — | Запросы от других агентов (in-process MCP) |
| **Communicator** | ЕДИНСТВЕННЫЙ, кто пишет в Mattermost. Injection-фильтр | — | Вызовы из агентов |
| **Dev (N штук)** | По одному на (репо, специализация) | Opus 4.8 (default) | `plan.ready` |
| **Reviewer** | Комменты в MR, апрувы, пинги, эскалация | Haiku 4.5 (lightweight) | Tick-поллинг |
| **DevOps** | CI/CD, красные пайплайны, auto-fix | — | Tick-поллинг |
| **ThreadResponder** | LLM-решение: ответить/внести правку/игнор | Opus 4.8 (default) | Вызовы из Reviewer/MmThreadListener |

**Message Bus**: SQLite-таблица `agent_messages` (durable, single-consumer per `to_agent`, `"*"` broadcast).
Topics: `task.discovered`, `plan.ready`, `mr.comment`, `mr.approved`, `mr.stuck`, `pipeline.failed`.

## Services (application/services/)
- **CommunicatorService** — запись в MM (DM, канал, реакции). Rate-limit sliding window. Working-hours gate.
- **InjectionFilter** — `<untrusted_content>`-обёртка с disarmed closing-тегом. Санитайз zero-width/bidi/tag-unicode. 5 классов инъекций.
- **Researcher** — in-process MCP сервер: `search_code` (git grep), `read_file`, `kb_search`, `kb_fetch_page_by_url`, `search_mr_history`.
- **AgentEffects** — DTO эффектов агента (ask_dispatched, plan_submitted, stuck, blocked).
- **AgentTrace** — structured audit-log событий (task_event, escalation, tool_call) для дашборда.
- **AnalystSessionRepository** — per-ticket состояние аналиста (awaiting DM, conversation log, fragments, deadlines).
- **PromptsLoader** — hot-reload системных промптов из `config/prompts/*.md` по mtime.
- **RulesLoader** — подгрузка `config/rules/<agent>.md` в system prompt агента.
- **RecoveryService** — восстановление после сбоев (re-publish missed events).
- **MrSummarizer** — генерация summary MR для Jira-коммента.
- **ReviewCommentClassifier** — эвристики: approval_hint / question / change_request / chatter.
- **LinkExtractor** — парсинг ссылок из Jira-описания.
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

**AnalystConversation** (а не дерево вопросов!): плоский append-only лог шагов аналиста на тикет. `ConversationStepKind`: PLANNER_DECIDED / BOT_ASKED / HUMAN_REPLIED / NOTE / STALE_FRAGMENT. Фрагменты сообщений буферизируются, после idle-окна (180s по умолчанию) → coalesce → HUMAN_REPLIED step → аналист перезапускается с полной историей.

## Repositories (7 total)
- `bellingshausen` — монорепа, backend + frontend (единственное активное сейчас)
- `rainbow`, `pts-aggregator`, `greeder` — backend
- `alertilka-backend`, `alertilka-deploy`, `alertilka-ui` — alertilka ecosystem

Добавляются динамически: строчка в `config/repositories.yaml` → перезапуск → появляются Dev-агенты.

## ORM Models + Alembic
9 таблиц в `infrastructure/db/models.py`: TaskRow, MergeRequestRow, AgentMessageRow, BusSubscriptionRow, PlanRow, MrHistoryRow, AnalystConversationStepRow, AnalystConversationFragmentRow, EventRow. 6 Alembic-миграций в `migrations/versions/` (0001–0006). Применяются через `Container._apply_migrations()` / `virtual-dev db init`.

## Integration Points
| System | Auth | Notes |
|--------|------|-------|
| Jira | PAT (Bearer token) | Статусы: `In Review`/`Closed` (не `Review`/`Done`). Transitions: to_in_progress → `In Progress`, to_review → `In Review`, to_testing → `Testing`, to_pending → `Waiting For Response`, to_done → `Closed` |
| GitLab | PAT | self-hosted, `draft: true` API-флаг silently дропается → `Draft:` префикс в title |
| Mattermost | PAT (driver.login) | WebSocket + REST. SSL verify=false |
| Confluence | PAT | CQL search, page fetch |

## Key Technical Decisions (see also `decisions-log.md`)
- Коммиты: `Virtual Dev <virtual-dev@datamining.2gis.ru>`, per-call `-c user.name/email` (не глобальный git config)
- Ветки: `ai-dev/<external_id>-<slug>`
- Workspace: уважает `local_path` из `repositories.yaml` (reuse чекаута) с safety-check на dirty tree (один раз на входе)
- MR draft через `Draft:` префикс (self-hosted GitLab дропает `draft: true`)
- Per-repo асинхронный lock (`asyncio.Lock`) для всех мутирующих git-ops (commit, push, checkout, merge)
- AnalystConversation: плоский лог вместо дерева вопросов. Coalescing 180s. Circuit breaker: `max_planner_calls_per_goal=8`, `max_goal_age_hours=48`.

## Development Environment
```bash
# Setup
uv sync                    # Установка зависимостей
cp .env.example .env       # Настройка секретов

# CLI
virtual-dev db init        # Инициализация БД (+ Alembic upgrade)
virtual-dev plan-task DM-1234 [--post]           # Запустить Analyst
virtual-dev dev-task DM-1234 --repo bellingshausen [--post]  # Запустить Dev
virtual-dev review-mrs     # One-shot Reviewer tick
virtual-dev watch-ci       # One-shot DevOps tick
virtual-dev index-mrs --repo <key>       # Индексация MR history для RAG

# Full system
virtual-dev run            # Web server + pollers

# Tests
uv run pytest              # 314+ unit tests (SDK/GitLab API не поднимаются)
```

## OpenCode Development Platform (`.opencode/`)
В репозитории синхронизирована платформа для AI-разработки. Это инфраструктура, в которой работает AI-агент. Virtual Dev — Python-проект, который эта платформа строит.

### Core Agents
- **OpenCoder** (`.opencode/agent/core/opencoder.md`) — системный промпт исполнителя задач. Workflow: Discover → Propose → Approve → Execute → Validate.
- **OpenAgent** (`.opencode/agent/core/openagent.md`) — универсальный агент для исследований.

### Subagents (вызываются через `task(subagent_type=...)`)
| Subagent | Назначение |
|----------|-----------|
| ContextScout | Поиск context-файлов по запросу |
| ExternalScout | Документация библиотек через Context7 |
| TaskManager | Разбивка фичи на subtask'и с зависимостями |
| CoderAgent | Исполнение одного coding subtask'a |
| BatchExecutor | Параллельное исполнение CoderAgent'ов |
| TestEngineer | Написание/прогон тестов |
| CodeReviewer | Ревью кода, безопасность |
| BuildAgent | Type check и сборка |
| DocWriter | Документация |
| DevOpsSpecialist | CI/CD, инфраструктура |
| FrontendSpecialist | UI/дизайн-системы |
| ContextOrganizer | Организация context-файлов |

### Commands (`/context`, `/commit`, `/test`, `/add-context`, `/clean`, `/optimize`, …)

### Skills
- **context7** — актуальная документация библиотек
- **task-management** — CLI для subtask'ов (router.sh + task-cli.ts)

### Context System
- Корень: `.opencode/context/` (настраивается в `config/paths.json`)
- Standards: `core/standards/` (code-quality, security-patterns, project-intelligence, …)
- Workflows: `core/workflows/` (component-planning, code-review, task-delegation, …)
- **project-intelligence/** — контекст Virtual Dev (этот файл и соседние)

## Related Files
- `business-domain.md` — Business context and team rules
- `decisions-log.md` — Decision rationale for architecture choices
- `living-notes.md` — Tech debt, open questions
