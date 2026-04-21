# Virtual Dev

Виртуальный AI-разработчик для команды DataMining (2GIS).

## Что это

Мульти-агентная система, которая:

1. Забирает задачи из Jira (по настраиваемому JQL).
2. Читает описание, связанные страницы Confluence и треды в Mattermost.
3. Строит план реализации. Если не хватает информации — задаёт вопросы коллегам.
4. Пишет код в нужном репозитории GitLab, открывает MR.
5. Ведёт цикл ревью: реагирует на комментарии, вносит правки.
6. Собирает апрувы, просит смержить.
7. Закрывает тикет.
8. Ведёт "книгу правил" на каждого dev-агента, обновляет её на основе замечаний ревьюеров.

## Архитектура

Hexagonal (ports & adapters). Подробно — в `docs/ARCHITECTURE.md`.

```
presentation  →  application (agents, workflows)  →  domain (models, ports)
                                                          ↑
                                                       adapters (jira, gitlab, mattermost, anthropic, ...)
```

Агенты общаются через in-process message bus (SQLite на старте).

## Агенты

- **Orchestrator** — маршрутизирует задачи, эскалирует, следит за таймаутами.
- **Analyst** — читает тикет, строит план.
- **Researcher** — ходит по Confluence / истории MR по требованию других агентов.
- **Communicator** — единственный, кто пишет в чат. Ведёт диалоги, фильтрует prompt injection.
- **Dev (N штук)** — по одному на (репо, специализация). Пишет код, гоняет тесты, создаёт MR.
- **Reviewer** — обрабатывает комментарии в открытых MR.
- **QA** — валидирует тесты.
- **DevOps** — CI/CD, красные пайплайны.

## Быстрый старт

Требования: Python 3.13+ (3.14 тоже работает), [uv](https://github.com/astral-sh/uv) и установленный Claude Code (`claude` CLI), в который ты уже залогинен через Claude Max. API-ключ Anthropic **не нужен** — бот работает через твою подписку.

```bash
# Установка uv (если ещё нет)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Зависимости
uv sync

# Конфиг
cp .env.example .env
# → вписать туда токены Jira/GitLab/Mattermost/Confluence

cp config/local.example.yaml config/local.yaml
# → проверить пути к репозиториям, JQL, рабочие часы

# Инициализация БД
uv run virtual-dev db init

# Прогнать Analyst на одном тикете (ничего не пишет в Jira)
uv run virtual-dev plan-task DM-1234

# Запуск дашборда + воркеров
uv run virtual-dev run
```

Дашборд откроется на http://localhost:8080.

## Конфигурация

- `config/repositories.yaml` — список репо, по которым работает бот. **Добавил строчку → перезапустил → появились агенты.**
- `config/agents.yaml` — настройки агентов: модели, лимиты, таймауты.
- `config/mappings.yaml` — email ↔ mattermost-username, jira-компонент ↔ репо.
- `config/local.yaml` — локальные переопределения (не в git).
- `config/rules/<agent>.md` — "книга правил" конкретного dev-агента.
- `.env` — секреты и URL сервисов.

## Фазы внедрения

- **Фаза 0 (текущая)** — скелет: читаем задачи из Jira, показываем в дашборде. Код не пишем, никому не пишем.
- **Фаза 1** — Analyst + Researcher + Communicator (read-only, только планы).
- **Фаза 2** — первый Dev-агент, пишет код на отобранных "чистых" тикетах.
- **Фаза 3** — Reviewer + DevOps, общение с людьми, полный цикл.
- **Фаза 4** — боевая обкатка на всех репо.
- **Фаза 5** — автопилот.

## Разработка

```bash
uv run pytest                      # тесты
uv run ruff check src tests        # линтер
uv run ruff format src tests       # форматтер
uv run mypy src                    # типы
```

## Безопасность

- Все входные данные от людей помечаются как untrusted и фильтруются перед подачей в LLM.
- Белый список репозиториев (из `config/repositories.yaml`).
- Лимиты токенов и итераций на задачу.
- Kill-switch в дашборде.
