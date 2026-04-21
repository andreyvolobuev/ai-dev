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
- `agents/orchestrator.py` — polls the tracker, upserts tasks, publishes `task.discovered`.
- `agents/analyst.py` — consumes `task.discovered`, gathers context, plans.
- `services/injection_filter.py` — wraps untrusted content in `<untrusted_content>`.
- `services/link_extractor.py` — buckets URLs in free-form text.
- `services/communicator.py` — Phase-1 read-only surface over `ChatPort`.
- `services/researcher.py` — exposes code grep / KB search as in-process MCP tools for the Analyst.

### `adapters/`
- `task_tracker/jira.py` — Jira (self-hosted) via `atlassian-python-api`.
- `chat/mattermost.py` — Mattermost (self-hosted), **read-only** in Phase 1 (`send_*` raises).
- `knowledge_base/confluence.py` — Confluence (self-hosted).
- `code_agent/claude_sdk.py` — `CodeAgentPort` via `claude-agent-sdk` (reuses the logged-in Claude Max CLI, no API key).
- `llm/claude_sdk.py` — single-shot `LlmPort` via the same SDK with tools disabled.
- `message_bus/sqlite.py` — durable SQLite-backed bus (production default). `message_bus/memory.py` — in-memory fallback for tests.
- `secrets/env.py` — reads from the process env.
- Stubs for `vcs/` — filled out in Phase 2.

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
Scheduler lives inside the FastAPI `lifespan` hook in Phase 1 — same event loop as the web server. Two tasks run in the background: the orchestrator poll loop and the Analyst agent runner (subscribed to the bus). A separate worker process will appear in Phase 2 when Dev-agents need long-running shell sessions.

- `runtime/workers/agent_runner.py` — generic subscribe-and-dispatch loop for one agent key.
- `runtime/workers/analyst_inbox.py` — `task.discovered` handler that runs AnalystAgent, then (optionally) transitions the Jira ticket to *In Progress* and comments the plan.

## Agents

| Agent | Phase | Role |
|---|---|---|
| Orchestrator | 0 | Polls Jira, persists tasks, publishes `task.discovered` |
| Analyst | 1 | Reads ticket + Confluence + MM threads, builds a plan |
| Researcher | 1 | In-process MCP toolkit (code grep / KB search) used by the Analyst |
| Communicator | 1 | Read-only MM access + injection filtering; no sends yet |
| Dev (×N) | 2 | One per (repo, specialisation); writes code, opens MR |
| Reviewer | 3 | Handles comments on open MRs |
| QA | 3 | Validates tests |
| DevOps | 3 | CI/CD, red pipelines |

Agents communicate **only** via `MessageBusPort`. Production uses the durable `SqliteMessageBus` (atomic claim via stamped `consumed_at`); the in-memory bus is retained for tests. Single-consumer per `to_agent` by convention; `"*"` fans out to every known subscriber.

## LLM integration

All Claude calls go through `claude-agent-sdk`, which spawns the locally installed `claude` CLI as a subprocess and reuses the logged-in Claude Max session. There is no direct dependency on the `anthropic` SDK and no API key. For simple single-shot calls the SDK is driven with `tools=[], max_turns=1` (see `ClaudeAgentSdkLlm`); for agent loops the Analyst mounts two in-process MCP servers (Researcher tools + a private `submit_plan` tool that captures structured output).

## Configuration

Two parallel sources, merged at startup:

- `.env` — secrets and per-machine runtime (URLs, tokens, DB path, dashboard port).
- `config/*.yaml` — what we work on (`repositories.yaml`), how agents behave (`agents.yaml`), identity maps (`mappings.yaml`). `local.yaml` provides per-machine overrides and is gitignored.

`local.yaml` beats all three base YAMLs. Env beats nothing — env values are pure secrets / infra.

## Data flow (Phase 1)

```
[Jira]
   │ poll (every N seconds)
   ▼
Orchestrator ── upsert ──► tasks table
   │
   │ publish "task.discovered"
   ▼
SqliteMessageBus ── subscribe ──► AnalystInbox
                                      │
                                      │ (transition → In Progress)
                                      ▼
                                 AnalystAgent
                                      │
                                      │ fetch Confluence / MM threads (read-only)
                                      │ wrap via InjectionFilter
                                      │ Claude Agent SDK session:
                                      │   - researcher tools (grep, kb_search, ...)
                                      │   - submit_plan tool → captures Plan
                                      ▼
                                   plans table
                                      │
                                      │ (comment plan summary)
                                      ▼
                                   [Jira]   &   [dashboard]
```

Writes in Phase 1: Jira transition + comment. **No MM writes.**

## Safety rails

- Every message originating outside the bot is marked `trusted=False`. Any LLM-facing code must run untrusted data through an injection filter (not yet implemented — Phase 1).
- Repositories are gated through `config/repositories.yaml`: no allowlist hit → no Dev-agent.
- Budgets (`DAILY_BUDGET_USD`, `PER_TASK_BUDGET_USD`, `PER_TASK_ITERATION_LIMIT`) are enforced by the `CodeAgentPort` adapter (Phase 2).
- Kill-switch: `POST /kill` stops the orchestrator; real wiring of all agents arrives in Phase 1.

## Testing strategy

- `tests/unit/` — domain models, services, agents with port fakes, adapters that can be exercised without network (SQLite bus, injection filter, researcher grep over a real tmp-path git repo).
- `tests/integration/` — reserved for adapters that need live services.
- `tests/e2e/` — reserved for `virtual-dev run` against a full local fixture.

Phase 1 suite: 44 unit tests. `claude-agent-sdk` calls are isolated behind `AnalystAgent._call_model`, which tests subclass and replace with canned submissions; real subprocess invocations of `claude` are never triggered in `uv run pytest`.
