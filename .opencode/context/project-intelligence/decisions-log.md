<!-- Context: project-intelligence/decisions | Priority: high | Version: 1.0 | Updated: 2026-07-01 -->

# Decisions Log

## Decision: LLM через Claude Max, не Anthropic API

**Date**: 2025-11 (Phase 0)
**Status**: Decided
**Owner**: Тимлид

**Context**: Нужно было выбрать, как использовать LLM. Anthropic API требует бюджет ($/токен), API-ключ, контроль лимитов. Claude Max — flat-rate подписка без per-token биллинга.

**Decision**: Работаем через Claude Max подписку пользователя. `claude-agent-sdk` (PyPI) → subprocess `claude` CLI → залогиненная сессия. API-ключ не используем.

**Rationale**: У Max нет лимитов на токены — только rate-limit на количество сообщений в 5-часовое окно. Это радикально упрощает архитектуру: не нужно tracking бюджета, throttling по cost, alarm'ов по превышению. Единственный лимит — `max_turns` (защита от runaway).

**Alternatives Rejected**:
| Альтернатива | Почему нет |
|-------------|------------|
| Anthropic API (per-token) | Нужен API-key, budget-трекинг, сложнее инфра |
| Self-hosted Llama | Пока нет infra для GPU, отложено |

**Impact**: + Максимально простая LLM-интеграция. Rate-limit обрабатывается retry-loop (2 попытки). Минус: нельзя использовать anthropic/python-sdk напрямую.

---

## Decision: Hexagonal Architecture (Ports & Adapters)

**Date**: 2025-11 (Phase 0)
**Status**: Decided

**Context**: Проект интегрируется с 4+ внешними системами (Jira, GitLab, Mattermost, Confluence). Они могут меняться.

**Decision**: Чёткое разделение на domain (модели+порты), application (агенты), adapters (реализации портов). Замена адаптера не трогает domain и application.

**Impact**: + Легко заменять внешние сервисы. Дороже на старте (интерфейсы), но окупается при смене интеграций.

---

## Decision: Bot Identity & Workspace Strategy

**Date**: 2026-04 (Phase 2)
**Status**: Decided

**Context**: Dev-агент должен писать код, коммитить и создавать MR от имени бота, не затирая работу человека.

**Decision**:
- Коммиты: `Virtual Dev <virtual-dev@datamining.2gis.ru>`, per-call `-c user.name/email` (не глобальный git config)
- Ветки: `ai-dev/<external_id>-<slug>`
- MR: draft (`Draft:` префикс, т.к. self-hosted GitLab дропает `draft: true` API-флаг)
- Workspace: уважает `local_path` из `repositories.yaml` (reuse чекаута). Safety-check на dirty tree один раз на входе.
- Per-repo `asyncio.Lock` для всех мутирующих git-ops.

**Impact**: + Безопасная работа рядом с человеком. + Никаких глобальных мутаций git config.

---

## Decision: Reviewer — Heuristic Comment Classification (пока)

**Date**: 2026-04 (Phase 3)
**Status**: Decided (Phase 5 → LLM)

**Context**: Reviewer должен классифицировать комментарии в MR: апрув, вопрос, change request, флейм.

**Decision**: Пока эвристики (`classify_comment` → `approval_hint`/`question`/`change_request`/`chatter`). Phase 5 заменит на LLM-классификацию.

**Impact**: + Просто и дёшево. − Ошибается на сложных/саркастичных комментариях.

---

## Decision: Reviewer CI Gate — не пинговать пока CI не зелёный

**Date**: 2026-04 (Phase 3.5.5)
**Status**: Decided

**Context**: Reviewer пинговал "please review" сразу после открытия MR, но CI часто был красным. Ревьюеры начинали смотреть, видели красный — теряли контекст.

**Decision**: Review-ping отправляется только когда CI SUCCESS/UNKNOWN. Гейт на `get_latest_pipeline_jobs` + `_collapse_status`, не на `mr.pipeline.status` (который desync'ится после push'а). `created`/`manual`/`skipped` статусы считаются "passing" (downstream deploy-гейты).

**Impact**: + Ревьюеры видят MR только когда код готов. + Никаких "посмотрю потом" из-за красного CI.

---

## Decision: DevOps Auto-Fix CI — молча, без шума в канал

**Date**: 2026-04 (Phase 3.5.5)
**Status**: Decided

**Context**: Раньше DevOps постил "Pipeline FAILED" в канал команды при каждом красном CI. Это создавало шум и не помогало.

**Decision**: Красный CI → бот МОЛЧА пытается починить (Dev.handle_iteration с полным логом). До `max_autofix_attempts=3` — никаких сообщений. После 3 неудач — DM тимлиду. **Канал команды вообще не видит CI-failures**.

**Impact**: + Нет шума в канале. + CI фиксится до того, как человек заметил. − Риск незаметного зацикливания (защита: max_attempts).

---

## Decision: MM WebSocket — Latency Optimization, Not Correctness

**Date**: 2026-04 (Phase 3.8.1)
**Status**: Decided

**Context**: WebSocket к Mattermost периодически падал (SSL, WAF, network issues). Каждое падение теряло сообщения.

**Decision**: WS — только для низкой задержки. Корректность через REST catch-up (`read_channel_since`, polling раз в 60s). WS-разрыв не теряет сообщения. `run_forever` с exponential backoff (5s→5min). Идемпотентность через ✅-реакцию / UNIQUE-индексы.

**Impact**: + WS может лежать час — сообщения не теряются. + Простая обработка ошибок (catch-up закроет gap за ≤1 минуту).

---

## Decision: Clarification Flow — Дерево Вопросов с Coalescing

**Date**: 2026-04 (Phase 3.7-3.8)
**Status**: Decided

**Context**: Нужен механизм, чтобы бот уточнял требования до того как писать код. Старый `ClarifierService` (один DM = один ответ) не работал с итеративными уточнениями.

**Decision**: Дерево вопросов (Question → Answer → Classification → Action). 6 типов классификации ответов. Coalescing: буферизируем fragments пока человек пишет, классифицируем после 600s idle. Loop guards: max_chain_depth=4, cycle detection, max_age=48h, max_subquestions=10.

**Impact**: + Естественный диалог (бот не перебивает). + Защита от runaway. + После ответов → перепланирование.

---

## Related Files
- `technical-domain.md` — Technical implementation details
- `living-notes.md` — Tech debt and deferrals
