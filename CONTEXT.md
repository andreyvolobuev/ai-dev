# Контекст проекта Virtual Dev

## Что это
Мульти-агентный AI-разработчик для команды DataMining (2GIS).
Подробнее — в README.md (появится на следующем шаге).

## История принятых решений

### Стек
- Python 3.13+ (на моей машине 3.14, но `requires-python = ">=3.13"` для совместимости зависимостей)
- Менеджер пакетов: uv
- LLM: Claude Sonnet 4.5 (основная), Haiku 4.5 (для лёгких задач типа "суммаризируй тред")
- Агентный фреймворк: Claude Agent SDK
- Трекер: Jira (self-hosted 2GIS)
- VCS: GitLab (self-hosted 2GIS)
- Чат: Mattermost (self-hosted 2GIS)
- KB: Confluence (self-hosted 2GIS)
- БД на старте: SQLite
- Web-дашборд: FastAPI + Jinja2
- CLI: typer

### Архитектура — hexagonal (ports & adapters)
Слои:
- `domain/` — модели и интерфейсы (ports). Без внешних зависимостей.
- `application/` — агенты, workflows, services. Зависят только от портов.
- `adapters/` — конкретные реализации портов (Jira, GitLab, Mattermost, Anthropic, ...).
- `infrastructure/` — БД, конфиг, DI, логи.
- `presentation/` — web-дашборд, CLI, webhooks.
- `runtime/` — воркеры, scheduler.

Смысл слоёв: чтобы завтра поменять Mattermost на Slack, Jira на Trello, облачный Claude на self-hosted Llama — надо менять ТОЛЬКО адаптер, не трогая domain и application.

### Агенты (мульти-агентная архитектура, каждый — отдельная сессия Claude Agent SDK со своим контекстом)
- **Orchestrator** — маршрутизирует задачи, эскалирует, держит только метаданные.
- **Analyst** — читает тикет + Confluence + MM-треды, строит план.
- **Researcher** — RAG по коду / истории MR / Confluence по запросу других агентов.
- **Communicator** — ЕДИНСТВЕННЫЙ, кто пишет в Mattermost. Фильтрует injection.
- **Dev (N штук)** — по одному на (репо, специализация: backend/frontend/devops). У каждого своя "книга правил".
- **Reviewer** — обрабатывает комменты в открытых MR.
- **QA** — валидация тестов.
- **DevOps** — CI/CD, красные пайплайны.

Агенты общаются через message bus (SQLite-таблица `messages` на старте, в будущем можно Redis/RabbitMQ).

### Репозитории
На старте — только `bellingshausen` (уже склонирован в `/Users/andreyvolobuev/Documents/2gis/bellingshausen`). Это монорепа, backend + frontend. Остальные 6 — подключим потом через `config/repositories.yaml`.

Все 7 репо:
- git@gitlab.2gis.ru:sd-data-mining/bellingshausen.git (backend + frontend)
- git@gitlab.2gis.ru:sd-data-mining/rainbow.git (backend)
- git@gitlab.2gis.ru:sd-data-mining/pts-aggregator.git (backend)
- git@gitlab.2gis.ru:sd-data-mining/greeder.git (backend)
- git@gitlab.2gis.ru:sd-data-mining/alertilka/alertilka-backend.git (backend)
- git@gitlab.2gis.ru:sd-data-mining/alertilka/alertilka-deploy.git (devops only)
- git@gitlab.2gis.ru:sd-data-mining/alertilka/alertilka-ui.git (frontend)

Репо добавляются динамически: строчка в `config/repositories.yaml` → перезапуск → появляются агенты.

### Общение с людьми (правила)
- Рабочие часы: 10:00–20:00 Мск, пн-пт.
- В первом сообщении треда/личке — дисклеймер про бота и команду `!ALARM` для остановки.
- Не дублировать дисклеймер в каждом сообщении.
- Кого спрашивать: вопросы по коду → git blame → автор; вопросы по бизнесу → командный канал.
- Timeout 4 часа в рабочее время → эскалация человеку (тимлиду).

### Безопасность
- Все входные данные от людей = untrusted, пропускаются через injection-фильтр.
- Белый список репо в конфиге.
- Лимиты: $5/задачу, 30 итераций/задачу, $20/день на весь бот.
- Kill-switch в дашборде.
- Секреты: на старте `.env`, потом Vault (есть в компании).

### JQL для отбора задач
Конфигурируемый. На старте:
`assignee = currentUser() AND labels = "ai-dev" AND status = "To Do"`
(пользователь пока использует свой аккаунт, без отдельного bot-юзера).

### Jira workflow
`To Do → In Progress → Review → Testing → Done`

### Самообучение
Markdown-файлы `config/rules/<agent>.md`. Подкладываются в system prompt. После задач агент предлагает дополнения, одобряет человек. Раз в неделю — дайджест тимлиду.

### Политика ревью
- Мержит человек (не бот) — осознанное решение.
- Бот пишет в канал "гляньте MR", пингует неотреагировавших.
- Когда собрал N апрувов — пишет "апрувы собрал, прошу смержить".

## Что уже сделано (Фаза 0, частично)

Создана структура директорий и файлы:
- `pyproject.toml` (uv + все зависимости + ruff/mypy/pytest)
- `README.md`
- `.gitignore`, `.env.example`
- `config/repositories.yaml` (bellingshausen раскомментирован, остальные — закомм. шаблоны)
- `config/agents.yaml`
- `config/mappings.yaml`
- `config/local.example.yaml`
- `config/rules/dev-bellingshausen-backend.md`
- `config/rules/dev-bellingshausen-frontend.md`
- Структура `src/virtual_dev/{domain,application,adapters,infrastructure,presentation,runtime}/` со всеми подпапками и `__init__.py`
- Модели домена: `domain/models/{task,repository,chat,merge_request,kb}.py`

## Что осталось для Фазы 0 (минимум, чтобы `uv run virtual-dev run` что-то делал)

1. **Ports** в `domain/ports/`:
   - `task_tracker.py` (fetch_tasks, transition, comment)
   - `vcs.py` (clone/pull, create_branch, commit, push, create_mr, list_comments, approve, merge)
   - `chat.py` (send_direct, send_to_channel, read_thread, find_user_by_email, subscribe)
   - `knowledge_base.py` (fetch_page, search)
   - `llm.py` (complete, stream)
   - `code_agent.py` (run_task — инкапсулирует Claude Agent SDK)
   - `secrets.py` (get)
   - `message_bus.py` (publish, subscribe)

2. **Первый адаптер — Jira** (`adapters/task_tracker/jira.py`), через `atlassian-python-api`.

3. **Infrastructure**:
   - `config/loader.py` — читает yaml + env с pydantic-settings
   - `container.py` — DI, связывает ports с adapters по конфигу
   - `db/` — SQLAlchemy модели (Task, MergeRequest, AgentMessage, Event), alembic init

4. **Минимальный Orchestrator** (`application/agents/orchestrator.py`), умеет:
   - Раз в 2 минуты дёргать TaskTrackerPort.fetch_tasks()
   - Писать новые задачи в БД
   - НЕ пишет в Jira, НЕ пишет в MM, НЕ пишет код (это Фаза 0)

5. **FastAPI-дашборд** (`presentation/web/`):
   - `GET /` — таблица задач
   - `GET /tasks/{id}` — детальная страница
   - `POST /kill` — kill-switch (на будущее)

6. **CLI** (`presentation/cli/main.py`):
   - `virtual-dev db init` — создаёт БД
   - `virtual-dev run` — запускает scheduler + дашборд

7. `docs/ARCHITECTURE.md`

8. Пара unit-тестов на доменные модели.

## Фазы после 0

- **Фаза 1** — Analyst + Researcher + Communicator (read-only, только строим планы, в Jira комментим, но в MM никому не пишем; команда видит планы в дашборде).
- **Фаза 2** — первый Dev-агент (bellingshausen backend) на отобранных "чистых" тикетах с полным DoD. Код + MR, но без ревью-цикла.
- **Фаза 3** — Reviewer + DevOps, общение с людьми в тестовом канале, полный цикл.
- **Фаза 4** — обкатка на реальных задачах всей команды.
- **Фаза 5** — автопилот, все репо, фронт-агенты.

## Стиль работы
- Пиши по-русски в ответах, в коде — английский (docstrings тоже английский, но допустимы русские комментарии для бизнес-контекста).
- Я (пользователь) — тимлид, знаю Python хорошо. Можно без "для чайников".
- Предпочитаю обсудить архитектурные решения перед тем как писать много кода.
- Fail loud, не глотай ошибки молча.
- Типизация строгая (mypy strict), pydantic для DTO, dataclasses для domain.