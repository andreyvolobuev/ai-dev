# Virtual Dev

Виртуальный AI-разработчик для команды DataMining (2GIS).

## Что это

Мульти-агентная система, которая:

1. Забирает задачи из Jira (по настраиваемому JQL).
2. Читает описание, вложения (PDF/DOCX/XLSX/изображения), связанные страницы Confluence, треды Mattermost, history MR.
3. Строит план реализации. Если не хватает информации — задаёт уточняющие вопросы постановщику в DM Mattermost и ждёт ответа.
4. По готовому плану пишет код в нужном репозитории GitLab через `claude-agent-sdk` и открывает draft MR.
5. Ведёт цикл ревью: реагирует на комментарии в MR, вносит правки, эскалирует молчание.
6. Следит за CI — пингует автора, если пайплайн стал красным.
7. Ведёт "книгу правил" на каждого dev-агента (`config/rules/`), которую можно править под фидбэк ревьюеров.

LLM работает через **Claude Max подписку** (CLI `claude` + `claude-agent-sdk`). API-ключ Anthropic не нужен.

## Архитектура

Hexagonal (ports & adapters). Подробно — в `docs/ARCHITECTURE.md`.

```
presentation/   FastAPI dashboard + Typer CLI
runtime/        планировщики и воркеры (живут в FastAPI lifespan)
application/    agents, services
domain/         models (dataclasses), ports (ABC)
                 ↑ реализуется ↓
adapters/       jira, gitlab, mattermost, confluence, claude-agent-sdk, sqlite-bus, ...
infrastructure/ config (env + YAML), DB (SQLAlchemy 2.0), DI-контейнер, logging
tools/          auto-discovery: каждый файл — отдельный SDK-tool, видимый агенту
```

Агенты общаются исключительно через `MessageBusPort` — на проде это durable SQLite-bus.

## Агенты

- **Orchestrator** — поллит Jira, апсёртит задачи, публикует `task.discovered`.
- **Analyst** — читает тикет + Confluence + MM-треды + код-базу, строит план, при нехватке инфы ходит в DM, по готовому плану публикует `plan.ready`.
- **Researcher** — внутренний MCP-toolkit (grep по коду, KB-поиск), вызывается Analyst'ом.
- **Communicator** — единственный, кто пишет в MM. Фильтрует prompt-injection, уважает рабочие часы.
- **Dev (×N)** — по одному на (репо, специализацию: backend/frontend/devops). Имплементит план, коммитит, открывает draft MR.
- **Reviewer** — обрабатывает новые комментарии в открытых MR, считает апрувы, эскалирует.
- **DevOps** — следит за CI; падение пайплайна — пинг автору.
- **Thread responder** — отвечает в тредах MM, где упомянули бота.

## Быстрый старт

Требования: Python 3.13+, [uv](https://github.com/astral-sh/uv), установленный и залогиненный CLI `claude` (Claude Max).

```bash
# Установка uv (если ещё нет)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Зависимости
uv sync

# Конфиг
cp .env.example .env
# → вписать туда токены Jira / GitLab / Mattermost / Confluence,
#   Mattermost-handle тимлида (ESCALATION_USER) и default-канал (DEFAULT_TEAM_CHANNEL).

# Инициализация SQLite (по умолчанию ./data/virtual_dev.db)
uv run virtual-dev db init

# Smoke-тест: один тик оркестратора, без записи куда-либо
uv run virtual-dev poll-once

# Запуск дашборда + всех воркеров в одном процессе
uv run virtual-dev run
```

Дашборд откроется на `http://127.0.0.1:8080` (хост/порт настраиваются через `WEB_HOST`/`WEB_PORT` или флаги `--host`/`--port`).

## CLI команды

Все команды — `uv run virtual-dev <cmd>` (или `virtual-dev <cmd>` если активирован venv).

| Команда | Что делает |
|---|---|
| `db init` | Создаёт таблицы SQLite по моделям SQLAlchemy. |
| `run [--host --port]` | Поднимает FastAPI-дашборд + scheduler + воркеров в одном процессе. |
| `poll-once` | Один проход Orchestrator — фетч из Jira, апсёрт в БД, диспатч `task.discovered`. Без побочных эффектов в Jira. |
| `plan-task DM-1234 [--post]` | Прогоняет Analyst на одном тикете. По умолчанию ничего не пишет в Jira; `--post` коммитит план как Jira-комментарий. |
| `dev-task DM-1234 --repo <key> [--spec backend\|frontend\|devops] [--post]` | Прогоняет Dev-агента на одном тикете. Требует готовый план в БД (`status=READY`) и `dor_satisfied=true`. |
| `review-mrs` | Один тик ReviewerAgent по всем открытым MR. |
| `watch-ci` | Один тик DevOpsAgent — проверка пайплайнов открытых MR. |
| `index-mrs --repo <key> [--limit 500]` | Строит/обновляет RAG-индекс по истории merged MR (fastembed, локально, ~220 MB модели в `~/.cache/fastembed`). |
| `clarifications show DM-1234` | Печатает timeline уточнений (BOT_ASKED → HUMAN_REPLIED → …) для тикета. |
| `test-analyst-ui [--port 8090]` | Поднимает изолированный web-UI для отладки Analyst — без Jira / GitLab / MM. См. ниже. |

## Тестовый UI для Analyst

`uv run virtual-dev test-analyst-ui` — standalone-страница на `http://127.0.0.1:8090`, на которой можно итеративно отлаживать поведение Analyst'а:

- слева — поле для текста "тикета";
- посередине — лента всех `tool_use` и промптов в реальном времени;
- справа — мок-чат: уточняющие вопросы прилетают сюда, можно отвечать и наблюдать, как Analyst коалесцирует ответы и продолжает работу.

Полезно, когда не хочется жечь живой Jira-тикет на каждой итерации промпта. Окно коалесцирования по умолчанию 30 секунд (на проде — 600); меняется флагом `--coalesce-seconds`.

## Конфигурация

| Где | Что |
|---|---|
| `.env` | Секреты + per-machine: токены, URL'ы, `WEB_HOST/PORT`, `DB_URL`, `WORKSPACES_DIR`, `REPO_LOCAL_PATHS`, `ESCALATION_USER`, `DEFAULT_TEAM_CHANNEL`. См. `.env.example`. |
| `config/repositories.yaml` | Список репо. Добавил строчку → перезапустил → появились новые dev-агенты. |
| `config/agents.yaml` | Параметры агентов: модели, лимиты итераций, таймауты. |
| `config/mappings.yaml` | email ↔ MM-username, jira-component ↔ репо. |
| `config/notifications.yaml` | Шаблоны и роутинг сообщений Communicator'a. |
| `config/prompts/*.md` | System-prompt'ы для Analyst / Dev / thread-responder. |
| `config/rules/<agent_key>.md` | "Книга правил" конкретного dev-агента. |
| `config/local.example.yaml` | Шаблон локальных оверрайдов; копировать как `local.yaml` (gitignored). |

`local.yaml` бьёт базовые YAML; `.env` — отдельный канал, исключительно для секретов и инфры.

## Инструменты агентов (`src/virtual_dev/tools/`)

Auto-discovery: каждый публичный `*.py` в этой директории — отдельный tool, видимый Claude. Чтобы добавить новый — кладёшь файл рядом, экспортируешь `build(ctx) -> SdkMcpTool`. Подробно — в `src/virtual_dev/tools/README.md`.

Текущие инструменты (выборочно): `read_jira_ticket`, `read_mattermost_thread`, `fetch_url`, `read_pdf_url`, `read_docx_url`, `read_xlsx_url`, `read_image_url`, `dm_user`, `lookup_chat_user`, `find_chat_user_by_name`, `search_code`, `search_mr_history`, `kb_search`, `read_file`, `submit_plan`, `submit_mr`, `submit_response`, `blocked`, `stuck`.

## Разработка

```bash
uv run pytest                          # все тесты
uv run pytest tests/unit               # только unit
uv run pytest -k analyst               # фильтр
uv run ruff check src tests            # линтер
uv run ruff format src tests           # форматтер
uv run mypy src                        # типы (strict)
```

Стиль: PEP8, line-length 100, double quotes (см. `[tool.ruff]` в `pyproject.toml`). Mypy — strict.

## Структура каталогов

```
src/virtual_dev/
  domain/         models/, ports/, events/, policies/
  application/    agents/, services/, workflows/
  adapters/       chat/, code_agent/, embedder/, knowledge_base/, llm/,
                  message_bus/, mr_history/, secrets/, task_tracker/, vcs/
  infrastructure/ config/, db/, logging/, container.py
  presentation/   cli/, web/ (FastAPI + Jinja2), webhooks/
  runtime/        scheduler/, workers/
  tools/          auto-discovered SDK tools

config/           YAML + prompts + rules
tests/            unit/, integration/, e2e/
docs/             ARCHITECTURE.md
data/             SQLite БД (по умолчанию)
workspaces/       checkout'ы репо для dev-агента (ignored)
```

## Безопасность

- Все входы от людей помечаются как untrusted и оборачиваются в `<untrusted_content>` перед подачей в LLM (`services/injection_filter.py`).
- Белый список репозиториев в `config/repositories.yaml` — ни в один другой репо бот не пушит.
- Лимиты токенов и итераций на задачу (`config/agents.yaml`).
- Kill-switch в дашборде (`/kill`).
- Push идёт под пользовательским `GITLAB_TOKEN`, но автор коммитов — всегда `DEV_GIT_AUTHOR_*`, чтобы в blame было видно, что это писал ИИ.

## Лицензия

MIT — см. [LICENSE.md](LICENSE.md).
