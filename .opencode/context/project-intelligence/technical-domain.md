<!-- Context: project-intelligence/technical | Priority: high | Version: 1.0 | Updated: 2026-07-01 -->

# Technical Domain

## Primary Stack
| Layer | Technology | Rationale |
|-------|-----------|-----------|
| Language | Python 3.13+ (>=3.13) | Совместимость зависимостей, на моей машине 3.14 |
| Package Manager | uv | Быстрее pip, единый формат |
| Agent Framework | Claude Agent SDK (`claude-agent-sdk` на PyPI) | Обёртка над `claude` CLI, через залогиненную Claude Max сессию |
| LLM | Claude Sonnet 4.5 (основная), Haiku 4.5 (лёгкие задачи) | Max подписка |
| Task Tracker | Jira (self-hosted, 2GIS) | `atlassian-python-api`, PAT-аутентификация (Bearer, не Basic) |
| VCS | GitLab (self-hosted, 2GIS) | `python-gitlab`, PAT |
| Chat | Mattermost (self-hosted, 2GIS, `mm.2gis.one`) | REST API + WebSocket, self-signed SSL |
| KB | Confluence (self-hosted, 2GIS) | REST API |
| DB | SQLite (async SQLAlchemy 2.0) | На старте |
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

## Architecture — Hexagonal (Ports & Adapters)
```
domain/         # Модели и интерфейсы (ports). Без внешних зависимостей.
application/    # Агенты, workflows, services. Зависят только от портов.
adapters/       # Реализации портов (Jira, GitLab, Mattermost, Confluence, ...)
infrastructure/ # БД, конфиг (pydantic-settings + yaml-loader), DI (Container), loguru
presentation/   # Web-дашборд (FastAPI+Jinja2), CLI (typer), webhooks
runtime/        # Воркеры: PollerWorker, AgentRunner, AnalystInbox, DevInbox, MmThreadListener
```

Смысл: замена адаптера (Mattermost→Slack, Jira→Trello) не трогает domain и application.

## Agents
Каждый — отдельная сессия Claude Agent SDK со своим контекстом.

| Agent | Role | Subscribed to |
|-------|------|---------------|
| **Orchestrator** | Маршрутизация, эскалация, metadata | Jira polling → `task.discovered` |
| **Analyst** | Читает тикет + Confluence + MM-треды, строит Plan | `task.discovered` |
| **Researcher** | RAG: git grep, read file, Confluence search | Запросы от других агентов (in-process MCP) |
| **Communicator** | ЕДИНСТВЕННЫЙ, кто пишет в Mattermost. Injection-фильтр | Вызовы из агентов |
| **Dev (N штук)** | По одному на (репо, специализация) | `plan.ready` |
| **Reviewer** | Комменты в MR, апрувы, пинги, эскалация | Tick-поллинг |
| **DevOps** | CI/CD, красные пайплайны, auto-fix | Tick-поллинг |
| **ThreadResponder** | LLM-решение: ответить/внести правку/игнор | Вызовы из Reviewer/MmThreadListener |
| **AnswerClassifier** | Классификация ответов в clarification flow | ClarificationOrchestrator |
| **CounterQuestionAnswerer** | Ответ на FACTUAL counter-Q (Sonnet 4.5) | ClarificationOrchestrator |
| **StakeholderResolver** | `@nick`/email/free-form → Mattermost user | ClarificationOrchestrator |

**Message Bus**: SQLite-таблица `messages` (durable, single-consumer per `to_agent`, `"*"` broadcast).
Topics: `task.discovered`, `plan.ready`, `mr.comment`, `mr.approved`, `mr.stuck`, `pipeline.failed`.

## Repositories (7 total)
- `bellingshausen` — монорепа, backend + frontend (единственное активное сейчас)
- `rainbow`, `pts-aggregator`, `greeder` — backend
- `alertilka-backend`, `alertilka-deploy`, `alertilka-ui` — alertilka ecosystem

Добавляются динамически: строчка в `config/repositories.yaml` → перезапуск → появляются Dev-агенты.

## Integration Points
| System | Auth | Notes |
|--------|------|-------|
| Jira | PAT (Bearer token) | Статусы: `In Review`/`Closed` (не `Review`/`Done`) |
| GitLab | PAT | self-hosted, `draft: true` API-флаг silently дропается → `Draft:` префикс в title |
| Mattermost | PAT (driver.login) | WebSocket + REST. SSL verify=false |
| Confluence | PAT | CQL search, page fetch |

## Key Technical Decisions (see also `decisions-log.md`)
- Коммиты: `Virtual Dev <virtual-dev@datamining.2gis.ru>`, per-call `-c user.name/email` (не глобальный git config)
- Ветки: `ai-dev/<external_id>-<slug>`
- Workspace: уважает `local_path` из `repositories.yaml` (reuse чекаута) с safety-check на dirty tree (один раз на входе)
- MR draft через `Draft:` префикс (self-hosted GitLab дропает `draft: true`)
- Per-repo асинхронный lock (`asyncio.Lock`) для всех мутирующих git-ops (commit, push, checkout, merge)

## Development Environment
```bash
# Setup
uv sync                    # Установка зависимостей
cp .env.example .env       # Настройка секретов

# CLI
virtual-dev db init        # Инициализация БД
virtual-dev plan-task DM-1234 [--post]           # Запустить Analyst
virtual-dev dev-task DM-1234 --repo bellingshausen [--post]  # Запустить Dev
virtual-dev review-mrs     # One-shot Reviewer tick
virtual-dev watch-ci       # One-shot DevOps tick
virtual-dev clarifications show DM-1234  # Дерево вопросов
virtual-dev index-mrs --repo <key>       # Индексация MR history для RAG

# Full system
virtual-dev run            # Web server + pollers

# Tests
uv run pytest              # 142+ unit tests (SDK/GitLab API не поднимаются)
```

## Related Files
- `business-domain.md` — Business context and team rules
- `decisions-log.md` — Decision rationale for architecture choices
- `living-notes.md` — Tech debt, open questions
