# Architecture

## Overview

Virtual Dev is structured as a **hexagonal (ports-and-adapters) application** with a multi-agent core. The point of the layering is pragmatic: replacing Mattermost with Slack, Jira with Trello, or Claude with a self-hosted model should touch exactly one adapter — nothing in `domain/` or `application/`.

## Layers

```
┌──────────────────────────────────────────────────────────────┐
│ presentation/   web dashboard (FastAPI) · CLI (typer)        │
├──────────────────────────────────────────────────────────────┤
│ runtime/        schedulers, workers                          │
├──────────────────────────────────────────────────────────────┤
│ application/    agents, workflows, domain services           │  ← depends only on domain
├──────────────────────────────────────────────────────────────┤
│ domain/         models (dataclasses), ports (ABCs)           │  ← zero external imports
└──────────────────────────────────────────────────────────────┘
                 ▲
                 │ implemented by
┌──────────────────────────────────────────────────────────────┐
│ adapters/       Jira, GitLab, Mattermost, Confluence, LLM    │
├──────────────────────────────────────────────────────────────┤
│ infrastructure/ config (env + YAML), DB, DI, logging         │
└──────────────────────────────────────────────────────────────┘
```

### `domain/`
- `models/` — dataclasses for `Task`, `Repository`, `ChatMessage`, `MergeRequest`, `KBPage`. No imports outside stdlib.
- `ports/` — ABCs that describe how the rest of the world looks from the application's point of view: `TaskTrackerPort`, `VcsPort`, `ChatPort`, `KnowledgeBasePort`, `LlmPort`, `CodeAgentPort`, `SecretsPort`, `MessageBusPort`.

### `application/`
- `agents/` — one Python class per agent (Orchestrator in Phase 0; Analyst, Communicator, Dev, Reviewer, QA, DevOps later). Each agent holds only ports as dependencies.
- `services/`, `workflows/` — business logic reusable across agents (not yet populated).

### `adapters/`
- `task_tracker/jira.py` — Jira (self-hosted) via `atlassian-python-api`.
- `message_bus/memory.py` — in-memory `asyncio.Queue` bus for Phase 0.
- `secrets/env.py` — reads from the process env.
- Stubs for `vcs/`, `chat/`, `knowledge_base/`, `llm/`, `code_agent/` — filled out in later phases.

### `infrastructure/`
- `config/settings.py` — pydantic-settings, reads `.env`.
- `config/loader.py` — YAML loader, merges `local.yaml` overrides.
- `config/schema.py` — pydantic schemas for each YAML file.
- `db/base.py` — async SQLAlchemy 2.0 engine + session.
- `db/models.py` — ORM rows (`TaskRow`, `MergeRequestRow`, `AgentMessageRow`, `EventRow`).
- `db/mappers.py` — domain ↔ ORM conversion.
- `container.py` — `Container` dataclass + `build_container()` wiring.
- `logging/` — loguru setup.

### `presentation/`
- `web/app.py` — FastAPI app with `/` (task list), `/tasks/{id}` (detail), `/kill`, `/healthz`.
- `web/templates/` — Jinja2 templates.
- `cli/main.py` — typer commands: `db init`, `run`, `poll-once`.

### `runtime/`
Scheduler lives inside the FastAPI `lifespan` hook in Phase 0 — same event loop as the web server. A separate worker process will appear in Phase 2 when Dev-agents need long-running shell sessions.

## Agents

| Agent | Phase | Role |
|---|---|---|
| Orchestrator | 0 | Polls Jira, persists tasks, routes & escalates later |
| Analyst | 1 | Reads ticket + Confluence + MM threads, builds a plan |
| Researcher | 1 | On-demand code/MR/Confluence RAG for other agents |
| Communicator | 1 | Sole writer to Mattermost; filters prompt injection |
| Dev (×N) | 2 | One per (repo, specialisation); writes code, opens MR |
| Reviewer | 3 | Handles comments on open MRs |
| QA | 3 | Validates tests |
| DevOps | 3 | CI/CD, red pipelines |

Agents communicate via `MessageBusPort`. Phase 0 ships an in-memory implementation; the SQLite-backed one arrives when the second agent does.

## Configuration

Two parallel sources, merged at startup:

- `.env` — secrets and per-machine runtime (URLs, tokens, DB path, dashboard port).
- `config/*.yaml` — what we work on (`repositories.yaml`), how agents behave (`agents.yaml`), identity maps (`mappings.yaml`). `local.yaml` provides per-machine overrides and is gitignored.

`local.yaml` beats all three base YAMLs. Env beats nothing — env values are pure secrets / infra.

## Data flow (Phase 0)

```
[Jira]  ◄── poll ── JiraTaskTracker ── map ──►  Task (domain)
                                                     │
                                                     ▼
                                        session_scope(factory)
                                                     │
                                                     ▼
                                                [tasks table]
                                                     │
                                              ◄─ select ─
                                                     │
                                              [dashboard]
```

No writes to Jira, no writes to Mattermost, no code generation. That's Phase 1+.

## Safety rails

- Every message originating outside the bot is marked `trusted=False`. Any LLM-facing code must run untrusted data through an injection filter (not yet implemented — Phase 1).
- Repositories are gated through `config/repositories.yaml`: no allowlist hit → no Dev-agent.
- Budgets (`DAILY_BUDGET_USD`, `PER_TASK_BUDGET_USD`, `PER_TASK_ITERATION_LIMIT`) are enforced by the `CodeAgentPort` adapter (Phase 2).
- Kill-switch: `POST /kill` stops the orchestrator; real wiring of all agents arrives in Phase 1.

## Testing strategy

- `tests/unit/` — domain models and pure logic; no network, no DB.
- `tests/integration/` — adapters against real in-memory stand-ins (SQLite, fake HTTP).
- `tests/e2e/` — full `virtual-dev poll-once` runs against a local fixture.

Currently we have unit tests for domain models only; the rest grows per phase.
