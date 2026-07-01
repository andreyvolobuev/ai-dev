<!-- Context: project-intelligence/notes | Priority: high | Version: 1.0 | Updated: 2026-07-01 -->

# Living Notes

## Technical Debt

| Item | Impact | Priority | Status |
|------|--------|----------|--------|
| Vault для секретов | Сейчас `.env` — небезопасно для продакшена | Low | Deferred (нужно выяснить какой Vault в компании) |
| Long-running stability | Нет метрик память/CPU/connections, WS не перезапускается автоматически | Medium | Deferred (когда появится продакшен-нагрузка) |
| Monitoring (Prometheus/Grafana) | Нет алертов, только loguru | Low | Deferred (при росте репо/команды) |
| E2E тесты | 142 unit теста, нет e2e с реальным GitLab/Jira/MM | Medium | Deferred (дорого собирать docker-compose) |
| Web dashboard | Базовая версия, нет таймлайна, override-кнопок | Low | Deferred (когда команда начнёт пользоваться) |
| Alembic migrations | БД пересоздаётся через `db init`, теряются данные | Medium | Deferred (пока dev, не прод) |

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
| 3.7 | ✅ | Clarification flow, 9 пунктов техдолга |
| 3.8 | ✅ | Clarification: доменная модель, coalescing, loop guards |
| 3.8.1 | ✅ | WS resilience: catch-up, run_forever |
| 4 | 🔄 | Обкатка на реальных задачах команды |
| 5 | ⏳ | Автопилот, все репо, фронт-агенты, LLM-классификация комментов |

## Patterns Worth Preserving

- **InjectionFilter**: все untrusted-данные оборачиваются в `<untrusted_content>` с disarmed closing-тегом
- **PromptsLoader**: hot-reload по `(name, mtime_ns)` — редактируешь промпт, без рестарта подхватывается
- **repositories_patch**: точечный patch одной репы по key в `config/local.yaml`, не replace всего списка
- **Message bus**: durable SQLite, single-consumer per `to_agent`, `"*"` broadcast
- **`_collapse_status`**: `created`/`manual`/`skipped` считаются passing (downstream deploy-гейты)

## Gotchas for Maintainers

- **`draft: true` API-флаг** self-hosted GitLab дропает молча → используем `Draft:` префикс в title
- **MR notes order**: `notes.list()` по умолчанию newest-first → всегда `order_by=created_at, sort=asc`
- **`mr.pipeline.status`** desync'ится после push'а (бывает None или старый статус) → используем `get_latest_pipeline_jobs` + `_collapse_status`
- **`log_tail_lines=0`** раньше означал "не качай лог" (баг) → семантика: `>0` tail, `<0` full, `0` skip
- **Jira transitions**: библиотека `set_issue_status` ищет `to == <status>` case-insensitive; если нет — поднимает ошибку со списком доступных
- **Circular import**: не делать eager `from .container import ...` в `infrastructure/__init__.py` → импортировать из `virtual_dev.infrastructure.container` явно

## What Works Well
- Hexagonal architecture: замена адаптеров без трогания domain
- Clarification flow: дерево вопросов эффективнее flat DM-обмена
- Silent auto-fix CI: команда не видит проблем, а CI зелёный

## Open Questions
- Vault: какой Vault в компании, как подключать?
- NFR: какие SLA/Metrics по времени ответа?
- Multi-user: как разделять контекст нескольких разработчиков?

## Related Files
- `business-domain.md` — Business constraints
- `technical-domain.md` — Technical implementation
- `decisions-log.md` — Decision history
