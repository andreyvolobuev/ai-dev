<!-- Context: project-intelligence/business | Priority: high | Version: 1.0 | Updated: 2026-07-01 -->

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

## Business Rules (Communication)
- **Рабочие часы**: 10:00–20:00 Мск, пн-пт. Вне часов — сообщения буферизуются (кроме `!ALARM`).
  Отключается `COMMUNICATOR_RESPECT_WORKING_HOURS=false`.
- **Дисклеймер**: В первом сообщении треда/личке — "я бот, напиши `!ALARM` чтобы остановить".
  Не дублировать в каждом сообщении.
- **Эскалация**: 4 часа без ответа в рабочее время → DM тимлиду.
- **Кого спрашивать**: вопросы по коду → git blame → автор; вопросы по бизнесу → командный канал.
- **Rate-limit**: Communicator имеет sliding window per target по `rate_limit_per_hour` из конфига.

## Jira Workflow
- JQL-фильтр: `assignee = currentUser() AND labels = "ai-dev" AND status = "To Do"`
- Статусы: `To Do → In Progress → In Review → Testing → Closed`
  (в DM-проекте `In Review`/`Closed`, не `Review`/`Done`)
- Пользователь — свой аккаунт (нет отдельного bot-юзера).

## Review Policy
- **Мержит человек** (не бот) — осознанное решение.
- Бот пингует ревьюеров: `ping_reviewers_after_hours` → пинг в канал; `escalate_after_hours` → DM тимлиду.
- Когда собрал N апрувов — пишет "апрувы собрал, прошу смержить".
- Review-ping **не отправляется**, пока CI не зелёный (бот ждёт).
- Единственный ручной шлюз на входе — метка `ai-dev` в Jira. На выходе — ревью MR человеком.

## Key Constraints
- Self-hosted инфра: GitLab (не GitHub), Mattermost (не Slack), Jira (не Linear).
- SSL-сертификаты self-signed — `MATTERMOST_SSL_VERIFY=false`.
- Все входные данные от людей = untrusted (injection-фильтр).
- Claude Max подписка — нет per-token биллинга (важно для архитектуры).

## Success Metrics
- Time from Jira `ai-dev` label → draft MR (target: <30 min for typical task).
- MR approval rate (target: >70% first-pass approval).
- Reduced CI-fix cycle time (auto-fix before human sees it).

## Related Files
- `technical-domain.md` — Stack, architecture, agents
- `decisions-log.md` — Key architectural decisions
- `living-notes.md` — Tech debt, roadmap
