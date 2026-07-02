<!-- Context: project-intelligence/bridge | Priority: high | Version: 1.1 | Updated: 2026-07-02 -->

# Business ↔ Tech Bridge

## Core Mapping

| Business Need | Technical Solution | Business Value |
|---------------|-------------------|----------------|
| Автоматизировать рутину разработчика | Multi-agent система: Analyst → Dev → Reviewer → DevOps | Разработчик занимается только ревью и сложными задачами |
| Не терять сообщения при падении WS | WS = latency optimization, REST catch-up для correctness | Никакие уведомления не пропадают |
| Не спамить канал команды | DevOps auto-fix: молча до 3 попыток, только DM тимлиду при неудаче | Канал видит MR 1 раз ("готово к ревью"), не видит CI-failures |
| Безопасность: не дать боту навредить | Injection-фильтр, workspace safety-check, max_turns, kill-switch | Можно оставить бота без присмотра |
| Адаптация к меняющимся требованиям | AnalystConversation: flat log + DM-уточнения + перепланирование | Бот уточняет неясное до написания кода |
| Эскалация застрявших MR | Reviewer: ping (4ч) → escalate (24ч) → DM тимлиду | MR не зависают на недели |

## Feature: AnalystConversation (уточнение требований)

**Business Context**:
- Проблема: Analyst строит план по неполному тикету → Dev пишет не то → переделки
- Решение: бот сам задаёт вопросы в Mattermost до того как запускать Dev

**Technical Implementation**:
- Analyst может отправить DM человеку через эффект `ask_dispatched` (MCP tool `dm_user`)
- Ответы буферизируются как `ConversationFragment`, ждут 180s тишины (coalescing)
- После coalescing → `HUMAN_REPLIED` step → аналист перезапускается с полной историей
- Circuit breaker: `max_planner_calls_per_goal=8`, `max_goal_age_hours=48`
- Никакой классификации ответов: аналист сам решает, что делать с полученной информацией

## Feature: Auto-Fix CI

**Business Context**:
- Проблема: красный CI отвлекает команду, создаёт шум
- Решение: бот фиксит сам, молча; канал не видит проблем

**Technical Implementation**:
- DevOps тикает (интервал из config) → полный лог упавших job'ов → Dev.handle_iteration
- 3 попытки → DM тимлиду. Счётчик сбрасывается на зелёном CI и при новом iteration из MM-треда.
- Падение CI = background task, не блочит поллер.

## Feature: Silent Iteration Push

**Business Context**:
- Проблема: бот писал "внёс правку" после каждого push'а, что создавало шум в чате, особенно при auto-fix CI

**Technical Implementation**:
- Push идёт молча. На следующем тике Reviewer видит CI зелёный и постит ack в канал фидбека.
- Ack идёт в тот же канал откуда пришёл фидбек (MM-тред ИЛИ GitLab MR коммент).

## Trade-off: Эвристики vs LLM для классификации комментов

- **Сейчас**: эвристики (`ReviewCommentClassifier`: approval_hint/question/change_request/chatter) — просто, быстро, но ошибается
- **Phase 5**: LLM-классификация — точнее, но дороже (каждый коммент = вызов модели)
- **Решение**: пока эвристики, LLM когда будет budget/performance OK

## Related Files
- `business-domain.md` — Business rules and stakeholders
- `technical-domain.md` — Technical architecture in detail
- `decisions-log.md` — Rationale for architectural decisions
