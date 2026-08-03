<!-- Context: project-intelligence/business | Priority: high | Version: 1.2 | Updated: 2026-07-06 -->

# Business Domain

## Project Identity
```
Project Name: Virtual Dev
Tagline: Мульти-агентный AI-разработчик для команды DataMining (2GIS)
Problem: Разработчики тратят ~40% времени на рутину: анализ тикетов, написание шаблонного кода,
        CI-фиксы, коммуникацию в Mattermost, ревью. Нужен бот, который автоматизирует
        полный цикл: Jira → анализ → код → MR → ревью → CI → мёрж.
Solution: Система специализированных AI-агентов (Analyst, Dev, Reviewer, DevOps, ...),
        работающих через Claude Agent SDK и message bus, интегрированных с Jira, GitLab,
        Mattermost, Confluence.
```

## Team & Stakeholders
- **Команда**: DataMining, 2GIS (self-hosted GitLab/Jira/Mattermost/Confluence)
- **Пользователь** (кто общается с ботом): тимлид команды, хорошо знает Python
- **Конечные пользователи**: разработчики DataMining (используют AI-агента через Jira-метки и MR)
- **Каналы связи**: Mattermost (чат), Jira (тикеты), GitLab (код/MR), Confluence (база знаний)

## Integrations (self-hosted 2GIS)

### Jira
- **URL**: `https://jira.2gis.ru`
- **Auth**: PAT (Bearer token, не Basic). Переключили с Basic на Bearer в Phase 2 smoke-test.
- **JQL-фильтр**: `assignee = currentUser() AND labels = "ai-dev" AND status = "To Do"`
- **Poll interval**: 120 секунд (Orchestrator)
- **Статусы DM-проекта**: `To Do → In Progress → In Review → Testing → Closed`
  (Важно: `In Review`/`Closed`, не `Review`/`Done` — конфиг поправлен)
- **Also**: `Waiting For Response` (для заблокированных задач)
- **Пользователь** — свой аккаунт (нет отдельного bot-юзера)
- **Transitions**: адаптер сам ищет `to == <status>` (case-insensitive); если нет — поднимает ошибку со списком доступных
- **Datetime**: Jira отдаёт tz-aware (`2025-07-30T18:54:30.000+07:00`). Все DateTime колонки в PG — `TIMESTAMPTZ` (миграция 0007)

### Mattermost
- **URL**: `https://mm.2gis.one` (не `mattermost.2gis.ru`!)
- **Auth**: PAT (`driver.login()`), eager login перед WS subscribe
- **SSL**: self-signed → `MATTERMOST_SSL_VERIFY=false` в env. Адаптер принимает `ssl_verify` + `ssl_ca_file`.
- **Write-side**: `send_direct` (через `create_direct_message_channel`), `send_to_channel`
- **WebSocket**: latency optimization (не correctness). REST catch-up (`read_channel_since`, 60s polling) закрывает gap. WS: exponential backoff 5s→5min, SSL order fix для Python 3.12+ (`check_hostname=False` перед `verify_mode=CERT_NONE`)
- **Bot identity в GitLab**: `@uk.datamining.aidev` (через `GITLAB_BOT_USERNAME`)

### Confluence
- **URL**: `https://confluence.2gis.ru` (self-hosted)
- **Auth**: PAT
- **API**: CQL search, `fetch_page`, `fetch_page_by_url` (парсит 3 вида URL), `search`

### GitLab
- **URL**: `https://gitlab.2gis.ru`
- **Auth**: PAT
- **MR draft**: `Draft:` префикс в title (self-hosted GitLab дропает `draft: true` API-флаг)
- **MR notes**: `order_by=created_at, sort=asc` (default desc ломает cutoff Reviewer'а)
- **Push retry**: до 3 раз с linear backoff на transient-маркеры (`Internal API unreachable`, connection reset, HTTP 5xx)

### LLM API Service
- **Корпоративный шлюз** (продакшн): `https://ai-openai-proxy.k8s.n3.2gis.io/anthropic`
  - Anthropic-compatible proxy, работает через `ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY`
  - Auth: `x-api-key` header (НЕ Bearer)
  - Только датированные model ID: `claude-opus-4-8`, `claude-haiku-4-5-20251001`
- **Локальная разработка**: Claude Max подписка через `claude` CLI (без шлюза, без API-ключа)
- **Переключение**: задание `ANTHROPIC_BASE_URL` в env включает шлюз; отсутствие — локальный Claude Max
- **Budget-лимиты**: нет (ни в Max, ни в шлюзе). Единственный лимит — `max_turns` (защита от runaway)

## Business Rules (Communication)
- **Рабочие часы**: 10:00–20:00 Мск, пн-пт. Вне часов — сообщения буферизуются (кроме `!ALARM`).
  Отключается `COMMUNICATOR_RESPECT_WORKING_HOURS=false`.
- **Дисклеймер**: В первом сообщении треда/личке — "я бот, напиши `!ALARM` чтобы остановить".
  Не дублировать в каждом сообщении.
- **Эскалация**: 4 часа без ответа в рабочее время → DM тимлиду.
- **Кого спрашивать**: вопросы по коду → git blame → автор; вопросы по бизнесу → командный канал.
- **Rate-limit**: Communicator имеет sliding window per target по `rate_limit_per_hour` из конфига (default 20).

## Review Policy
- **Мержит человек** (не бот) — осознанное решение.
- **Required approvals**: 1
- **Ping reviewers**: через 4 часа после открытия MR → пинг в канал
- **Escalate**: через 24 часа без прогресса → DM тимлиду
- Review-ping **не отправляется**, пока CI не зелёный (бот ждёт).
- Единственный ручной шлюз на входе — метка `ai-dev` в Jira. На выходе — ревью MR человеком.

## Deployment
- **K8s namespace**: `sd-bots-liza`
- **Cluster**: `https://master.k8s.2gis.dev:6443`
- **External URL**: `ai-dev-sd-bots-liza.istio.k8s.2gis.dev` (HTTPRoute → Istio Gateway)
- **Деплой-репо**: `sd-bots-ai-dev-ai-dev` (Helm chart, отдельный репо)
- **CI/CD**: GitLab CI, `helm upgrade --install --atomic --wait --timeout 5m`
- **Image**: `docker-hub.2gis.ru/sd-bots/sd-bots-ai-dev-ai-dev:<tag>`
- **Env**: CI-раннер инжектирует ENV_KEYS (24 переменных) + SECRET_KEYS (5 переменных) через `build-values.py`
- **Resources**: requests 100m/256Mi, limits 500m/1Gi

## Key Constraints
- Self-hosted инфра: GitLab (не GitHub), Mattermost (не Slack), Jira (не Linear).
- SSL-сертификаты self-signed — `MATTERMOST_SSL_VERIFY=false`.
- Все входные данные от людей = untrusted (injection-фильтр).
- Claude Max подписка — нет per-token биллинга (важно для архитектуры).
- Корпоративный прокси (`ANTHROPIC_BASE_URL`) — требуются датированные model ID.

## Success Metrics
- Time from Jira `ai-dev` label → draft MR (target: <30 min for typical task).
- MR approval rate (target: >70% first-pass approval).
- Reduced CI-fix cycle time (auto-fix before human sees it).

## Related Files
- `technical-domain.md` — Stack, architecture, agents
- `decisions-log.md` — Key architectural decisions
- `living-notes.md` — Tech debt, roadmap
