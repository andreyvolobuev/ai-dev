<!-- Context: project-intelligence/notes | Priority: high | Version: 1.2 | Updated: 2026-07-06 -->

# Living Notes

## Technical Debt

| Item | Impact | Priority | Status |
|------|--------|----------|--------|
| Vault для секретов | Сейчас `.env` + K8s Secrets — небезопасно для продакшена | Low | Deferred |
| Alembic миграции на проде | Сейчас `db init` (create_all идемпотентен). Нужны нормальные миграции с data-preservation | Medium | Partially done (7 миграций, но нет rollback-test) |
| Long-running stability | Нет метрик память/CPU/connections | Medium | Deferred (когда появится продакшен-нагрузка) |
| Monitoring (Prometheus/Grafana) | Нет алертов, только loguru | Low | Deferred |
| E2E тесты | 314 unit, нет e2e с реальным GitLab/Jira/MM | Medium | Deferred |
| Web dashboard | Базовая версия, нет таймлайна, override-кнопок | Low | Deferred |
| `healthcheckPath` в Helm | readinessProbe opt-in, должен быть всегда `/healthz` | High | TODO в деплой-репо |
| `WEB_HOST` в `build-values.py` | Мёртвый код, `--host 0.0.0.0` в CMD уже overrides | Low | TODO в деплой-репо |

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
| 5.0 | ✅ | AnalystConversation: flat log + coalescing. MCP tools. Alembic миграции |
| 4-deploy | ✅ | PostgreSQL migration, Docker, K8s deployment (Helm) |
| 4 | 🔄 | Обкатка на реальных задачах команды в K8s |
| 5 | ⏳ | Автопилот, все репо, фронт-агенты, LLM-классификация комментов |

## Patterns Worth Preserving

- **InjectionFilter**: все untrusted-данные в `<untrusted_content>` с disarmed closing-тегом
- **PromptsLoader**: hot-reload по `(name, mtime_ns)` — редактируешь промпт, без рестарта
- **repositories_patch**: точечный patch одной репы по key в `config/local.yaml`
- **Message bus**: `SqlAlchemyMessageBus` (PG-backed), dialect-aware upsert, single-consumer per `to_agent`, `"*"` broadcast
- **`_collapse_status`**: `created`/`manual`/`skipped` считаются passing (downstream deploy-гейты)
- **MCP tools авто-discovery**: модуль с `build(ctx) -> SdkMcpTool | None` — сам регистрируется
- **AnalystConversation flat log**: append-only лог + фрагменты + coalescing. Нет state machine.
- **`init_db()` в lifespan**: миграции накатываются при старте пода, 30s timeout guard
- **Docker two-layer cache**: deps layer (pyproject.toml + uv.lock) → source layer. Non-root user.

## Gotchas for Maintainers

- **LLM-шлюз vs Claude Max**: `ANTHROPIC_BASE_URL` в env → шлюз (`ai-openai-proxy.k8s.n3.2gis.io/anthropic`), нет → локальный Claude Max. Шлюз: auth через `x-api-key` (НЕ Bearer — даёт 401), `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` обязателен (режет `context_management` и т.д.).
- **Модели**: `claude-opus-4-8` (основная, работает везде), `claude-haiku-4-5-20251001` (лёгкая, датированный ID обязателен для шлюза). Короткие алиасы дают 404 на шлюзе.
- **Mattermost URL**: `mm.2gis.one` (не `mattermost.2gis.ru`). Self-signed SSL → `MATTERMOST_SSL_VERIFY=false`.
- **`draft: true` API-флаг** self-hosted GitLab дропает молча → `Draft:` префикс в title
- **MR notes order**: `notes.list()` по умолчанию newest-first → всегда `order_by=created_at, sort=asc`
- **`mr.pipeline.status`** desync'ится после push'а → используем `get_latest_pipeline_jobs` + `_collapse_status`
- **Jira transitions**: `set_issue_status` ищет `to == <status>` case-insensitive; нет — поднимает ошибку. DM-проект: `In Review`/`Closed` (не `Review`/`Done`).
- **Jira datetime**: tz-aware (`+07:00`). Все DateTime колонки — `TIMESTAMPTZ` (миграция 0007).
- **asyncpg strict typing**: не прощает tz-aware vs `TIMESTAMP WITHOUT TIME ZONE` (в отличие от SQLite). Все DateTime → `DateTime(timezone=True)`.
- **`DB_DSN` не `DB_URL`**: драйвер `postgresql+asyncpg://`. Alembic стрипает `+asyncpg` → `postgresql://` + `psycopg2-binary`.
- **`WEB_HOST` в K8s**: ConfigMap инжектирует `127.0.0.1` → `--host 0.0.0.0` в CMD overrides (CLI arg > env).
- **`init_db()` timeout**: 30s. Если БД недоступна — под стартует без миграций (логирует error). readinessProbe покажет реальное состояние.
- **Circular import**: не делать eager `from .container import ...` в `infrastructure/__init__.py`
- **AnalystConversation**: плоский лог, НЕ дерево. Нет `Question`/`Answer`/`Stakeholder` доменных моделей.
- **Tests**: 314 unit-тестов. `uv run pytest`. SDK/GitLab API не поднимаются — всё через фейки.
- **Docker CMD**: `virtual-dev` напрямую (не `uv run`), т.к. `.venv/bin` в `PATH`. `--no-dev` при sync, но не при run.

## What Works Well
- Hexagonal architecture: замена адаптеров без трогания domain
- AnalystConversation flat log: проще, чем дерево вопросов
- Silent auto-fix CI: команда не видит проблем, а CI зелёный
- MCP tools авто-discovery: новый tool = новый файл, без регистрации
- `init_db()` в lifespan: ноль ручных шагов при деплое

## Open Questions
- Vault: какой Vault в компании, как подключать?
- NFR: какие SLA/Metrics по времени ответа?
- Multi-user: как разделять контекст нескольких разработчиков?
- Helm: включить `healthcheckPath: /healthz` permanently? Убрать `WEB_HOST` из `build-values.py`?

## Related Files
- `business-domain.md` — Business constraints
- `technical-domain.md` — Technical implementation
- `decisions-log.md` — Decision history
