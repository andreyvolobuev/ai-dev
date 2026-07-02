<!-- Context: project-intelligence/notes | Priority: high | Version: 1.1 | Updated: 2026-07-02 -->

# Living Notes

## Technical Debt

| Item | Impact | Priority | Status |
|------|--------|----------|--------|
| Vault для секретов | Сейчас `.env` — небезопасно для продакшена | Low | Deferred (нужно выяснить какой Vault в компании) |
| Long-running stability | Нет метрик память/CPU/connections, WS не перезапускается автоматически | Medium | Deferred (когда появится продакшен-нагрузка) |
| Monitoring (Prometheus/Grafana) | Нет алертов, только loguru | Low | Deferred (при росте репо/команды) |
| E2E тесты | 314 unit теста, нет e2e с реальным GitLab/Jira/MM | Medium | Deferred (дорого собирать docker-compose) |
| Web dashboard | Базовая версия, нет таймлайна, override-кнопок | Low | Deferred (когда команда начнёт пользоваться) |

## Roadmap

| Phase | Status | What |
|-------|--------|------|
| 0 | ✅ | Скелет, Jira polling, domain-модели, 8 ports |
| 1 | ✅ | Analyst + Researcher + Communicator (read-only) |
| 2 | ✅ | Dev-агент, GitLab VCS, workspace, draft MR |
| 2.5 | ✅ | RAG по истории MR (Fastembed + ONNX) |
| 3 | ✅ | Reviewer + DevOps + write-side Communicator |
| 3.5 | ✅ | MM-тред как канал ревью, WebSocket listener |
| 3.5.5 | ✅ | Шаблоны/промпты в конфиг, auto-fix CI |
| 3.6 | ✅ | Silent push, GitLab комменты → ThreadResponder |
| 3.8.1 | ✅ | WS resilience: catch-up, run_forever |
| 5.0 | ✅ | AnalystConversation: flat log + coalescing вместо clarification tree. MCP tools system. Alembic миграции |
| 4 | 🔄 | Обкатка на реальных задачах команды |
| 5 | ⏳ | Автопилот, все репо, фронт-агенты, LLM-классификация комментов |

## Patterns Worth Preserving

- **InjectionFilter**: все untrusted-данные оборачиваются в `<untrusted_content>` с disarmed closing-тегом
- **PromptsLoader**: hot-reload по `(name, mtime_ns)` — редактируешь промпт, без рестарта подхватывается
- **repositories_patch**: точечный patch одной репы по key в `config/local.yaml`, не replace всего списка
- **Message bus**: durable SQLite, single-consumer per `to_agent`, `"*"` broadcast
- **`_collapse_status`**: `created`/`manual`/`skipped` считаются passing (downstream deploy-гейты)
- **MCP tools авто-discovery**: модуль с `build(ctx) -> SdkMcpTool | None` в `src/virtual_dev/tools/` — сам регистрируется. Группировка по `TOOL_GROUP`.
- **AnalystConversation flat log**: append-only лог шагов + фрагменты + coalescing. Никакой state machine для классификации ответов.
- **Alembic на старте**: 6 миграций, upgrade_to_head в контейнере. `db init` = migrate.

## OpenCode Platform — Available Tools

Синхронизирована полная платформа `.opencode/`. Для работы с Virtual Dev доступны:

**Subagents** (через `task(subagent_type=...)`):
- `ContextScout` — найти релевантные context-файлы
- `ExternalScout` — свежая документация библиотек
- `TaskManager` — разбить задачу на subtask'и
- `CoderAgent` / `BatchExecutor` — параллельное исполнение кода
- `TestEngineer` — тесты
- `CodeReviewer` — ревью кода
- `ContextOrganizer` — организация context

**Commands**: `/context`, `/commit`, `/test`, `/add-context`, `/clean`, `/optimize`

**Skills**: context7 (документация), task-management (CLI для subtask'ов)

**Context**: `.opencode/context/core/standards/code-quality.md` (MANDATORY before code work), `project-intelligence/` (этот проект), `core/workflows/` (workflow'ы)

## Gotchas for Maintainers

- **Модели Opus 4.8 / Haiku 4.5**: при корпоративном прокси (`ANTHROPIC_BASE_URL`) требуются датированные ID. `claude-opus-4-8` работает везде. `claude-haiku-4-5` без даты может дать 404 → используем `claude-haiku-4-5-20251001`.
- **`draft: true` API-флаг** self-hosted GitLab дропает молча → используем `Draft:` префикс в title
- **MR notes order**: `notes.list()` по умолчанию newest-first → всегда `order_by=created_at, sort=asc`
- **`mr.pipeline.status`** desync'ится после push'а (бывает None или старый статус) → используем `get_latest_pipeline_jobs` + `_collapse_status`
- **`log_tail_lines=0`** раньше означал "не качай лог" (баг) → семантика: `>0` tail, `<0` full, `0` skip
- **Jira transitions**: библиотека `set_issue_status` ищет `to == <status>` case-insensitive; если нет — поднимает ошибку со списком доступных
- **Circular import**: не делать eager `from .container import ...` в `infrastructure/__init__.py` → импортировать из `virtual_dev.infrastructure.container` явно
- **AnalystConversation vs Clarification**: в коде нет `Question`/`Answer`/`Stakeholder` доменных моделей. Нет `AnswerClassifier`/`CounterQuestionAnswerer`/`StakeholderResolver` агентов. Вместо этого — плоский лог с coalescing в `AnalystInbox`.
- **Tests**: 314 unit-тестов. `uv run pytest` собирает тесты из `tests/unit/`. На CI не поднимаются SDK/GitLab API — всё через фейки.

## What Works Well
- Hexagonal architecture: замена адаптеров без трогания domain
- AnalystConversation flat log: проще, чем дерево вопросов
- Silent auto-fix CI: команда не видит проблем, а CI зелёный
- MCP tools авто-discovery: новый tool = новый файл, без регистрации

## Open Questions
- Vault: какой Vault в компании, как подключать?
- NFR: какие SLA/Metrics по времени ответа?
- Multi-user: как разделять контекст нескольких разработчиков?

## Related Files
- `business-domain.md` — Business constraints
- `technical-domain.md` — Technical implementation
- `decisions-log.md` — Decision history
