<!-- Context: project-intelligence/decisions | Priority: high | Version: 1.2 | Updated: 2026-07-06 -->

# Decisions Log

## Decision: LLM — два режима (Claude Max + корпоративный шлюз)

**Date**: 2025-11 (Phase 0), обновлено 2026-07 (Phase 4 — K8s deploy)
**Status**: Decided

**Context**: Нужно было выбрать, как использовать LLM. Anthropic API требует бюджет ($/токен), API-ключ. Claude Max — flat-rate подписка без per-token биллинга. Для продакшена в K8s нужен автономный режим без личной подписки.

**Decision**: Два режима работы, переключаемых через env:
1. **Claude Max** (локальная разработка): `claude-agent-sdk` → `claude` CLI → залогиненная сессия. Без API-ключа.
2. **Корпоративный шлюз** (продакшн/K8s): `ANTHROPIC_BASE_URL=https://ai-openai-proxy.k8s.n3.2gis.io/anthropic` + `ANTHROPIC_API_KEY`. Anthropic-compatible proxy, auth через `x-api-key` (не Bearer). `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` (строгий шлюз режет beta-поля). Только датированные model ID.

**Rationale**: У Max и у шлюза нет per-token биллинга — не нужен budget-трекинг. Шлюз позволяет работать в K8s без личной подписки. `_build_claude_env()` в `container.py` автоматически переключает режимы.

**Impact**: + Один код, два режима. + Нет budget-трекинга. − Шлюз требует датированные model ID (алиасы дают 404). − Шлюз режет beta-поля (`context_management` и т.д.).

---

## Decision: Hexagonal Architecture (Ports & Adapters)

**Date**: 2025-11 (Phase 0)
**Status**: Decided

**Context**: Проект интегрируется с 4+ внешними системами (Jira, GitLab, Mattermost, Confluence). Они могут меняться.

**Decision**: Чёткое разделение на domain (модели+порты), application (агенты), adapters (реализации портов).

**Impact**: + Легко заменять внешние сервисы. Дороже на старте, но окупается при смене интеграций.

---

## Decision: Bot Identity & Workspace Strategy

**Date**: 2026-04 (Phase 2)
**Status**: Decided

**Decision**:
- Коммиты: `Virtual Dev <virtual-dev@datamining.2gis.ru>`, per-call `-c user.name/email`
- Ветки: `ai-dev/<external_id>-<slug>`
- MR: draft (`Draft:` префикс, т.к. self-hosted GitLab дропает `draft: true`)
- Workspace: уважает `local_path` из `repositories.yaml`, safety-check на dirty tree один раз на входе
- Per-repo `asyncio.Lock` для всех мутирующих git-ops

**Impact**: + Безопасная работа рядом с человеком. + Никаких глобальных мутаций git config.

---

## Decision: PostgreSQL вместо SQLite

**Date**: 2026-07 (Phase 4 — deployment)
**Status**: Decided

**Context**: SQLite не подходит для продакшена в K8s: нет concurrent writes, file-based, нет connection pooling. Нужна "настоящая" БД для multi-past deploy.

**Decision**: PostgreSQL 16, asyncpg driver. `DB_DSN` вместо `DB_URL`. `SqlAlchemyMessageBus` с dialect-aware upsert. `psycopg2-binary` для sync Alembic.

**Rationale**: asyncpg строгий к типам (в отличие от SQLite) — выявил mismatch tz-aware datetime vs `TIMESTAMP WITHOUT TIME ZONE`. Это заставило сделать миграцию 0007 (`TIMESTAMPTZ`).

**Impact**: + Production-grade БД. + Connection pooling. − Двойной driver (asyncpg + psycopg2). − Строгая типизация выявила скрытые баги (tz mismatch).

---

## Decision: TIMESTAMPTZ для всех DateTime колонок

**Date**: 2026-07 (Phase 4)
**Status**: Decided

**Context**: Приложение везде генерирует tz-aware datetime (`datetime.now(timezone.utc)`, Jira отдаёт `+07:00`). Колонки были `TIMESTAMP WITHOUT TIME ZONE`. SQLite прощал mismatch, asyncpg — нет: `can't subtract offset-naive and offset-aware datetimes`.

**Decision**: Все `DateTime` → `DateTime(timezone=True)` в ORM. Миграция 0007 конвертирует `TIMESTAMP` → `TIMESTAMPTZ` (Postgres only, SQLite no-op), интерпретируя существующие значения как UTC.

**Impact**: + asyncpg работает корректно. + Jira datetimes сохраняются с оригинальным tz. − Миграция на проде (но идемпотентна).

---

## Decision: Docker + K8s Deployment

**Date**: 2026-07 (Phase 4)
**Status**: Decided

**Context**: Приложение должно работать автономно в корпоративном K8s. До этого — локальный `virtual-dev run` на машине тимлида.

**Decision**:
- `Dockerfile`: корпоративный registry, uv, non-root user, `CMD ["virtual-dev", "run", "--host", "0.0.0.0"]`
- `init_db()` в FastAPI `lifespan` с 30s timeout guard — миграции накатываются при старте пода
- Helm chart в отдельном репо (`sd-bots-ai-dev-ai-dev`)
- `readinessProbe: /healthz`, `helm --atomic --wait --timeout 5m`
- `--host 0.0.0.0` в CMD overrides `WEB_HOST` из ConfigMap (CLI arg > env)

**Rationale**: `init_db()` в lifespan вместо `initContainer` — нет доступа к деплой-файлам на момент реализации. Timeout guard защищает от зависания при недоступной БД.

**Impact**: + Автоматический деплой без ручных шагов. + Миграции накатываются автоматически. − Если БД недоступна >30s — под стартует без миграций (но readinessProbe покажет реальное состояние).

---

## Decision: Reviewer CI Gate — не пинговать пока CI не зелёный

**Date**: 2026-04 (Phase 3.5.5)
**Status**: Decided

**Decision**: Review-ping отправляется только когда CI SUCCESS/UNKNOWN. Гейт на `get_latest_pipeline_jobs` + `_collapse_status`. `created`/`manual`/`skipped` считаются "passing".

---

## Decision: DevOps Auto-Fix CI — молча, без шума в канал

**Date**: 2026-04 (Phase 3.5.5)
**Status**: Decided

**Decision**: Красный CI → бот МОЛЧА пытается починить (Dev.handle_iteration с полным логом). До 3 попыток — никаких сообщений. После — DM тимлиду. Канал не видит CI-failures.

---

## Decision: MM WebSocket — Latency Optimization, Not Correctness

**Date**: 2026-04 (Phase 3.8.1)
**Status**: Decided

**Decision**: WS — только для низкой задержки. Корректность через REST catch-up (`read_channel_since`, 60s polling). WS-разрыв не теряет сообщения. `run_forever` с exponential backoff 5s→5min.

---

## Decision: AnalystConversation — Flat Log + Coalescing

**Date**: 2026-06 (Phase 5.0)
**Status**: Decided

**Decision**: Плоский append-only лог (`ConversationStep`) + буферизированные фрагменты. Coalescing 180s → merge → HUMAN_REPLIED → перезапуск аналиста. Circuit breaker: `max_planner_calls_per_goal=8`.

**Impact**: + Радикально проще (один AnalystInbox вместо 3 агентов). + Меньше LLM-вызовов.

---

## Related Files
- `technical-domain.md` — Technical implementation details
- `living-notes.md` — Tech debt and deferrals
