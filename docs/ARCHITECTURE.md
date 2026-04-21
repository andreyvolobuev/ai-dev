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
- `agents/analyst.py` — consumes `task.discovered`, gathers context, plans; on a READY plan publishes `plan.ready` addressed to the target Dev-agent.
- `agents/dev.py` — consumes `plan.ready`, implements the plan in a dedicated workspace via Claude Code tools, commits + pushes a branch, opens a draft MR. Gated on `task.dor_satisfied`. Four terminal outcomes: `SKIPPED` / `NO_CHANGES` / `MR_OPENED` / `FAILED`.
- `services/injection_filter.py` — wraps untrusted content in `<untrusted_content>`.
- `services/link_extractor.py` — buckets URLs in free-form text.
- `services/communicator.py` — Phase-1-2 read-only surface over `ChatPort`.
- `services/researcher.py` — exposes code grep / KB search as in-process MCP tools for the Analyst.
- `services/rules.py` — loads `config/rules/<agent_key>.md` for the Dev-agent's system prompt.

### `adapters/`
- `task_tracker/jira.py` — Jira (self-hosted) via `atlassian-python-api`.
- `chat/mattermost.py` — Mattermost (self-hosted), **read-only** in Phase 1-2 (`send_*` raises).
- `knowledge_base/confluence.py` — Confluence (self-hosted).
- `code_agent/claude_sdk.py` — `CodeAgentPort` via `claude-agent-sdk` (reuses the logged-in Claude Max CLI, no API key).
- `llm/claude_sdk.py` — single-shot `LlmPort` via the same SDK with tools disabled.
- `vcs/gitlab.py` — `VcsPort` via `python-gitlab` for remote API + plain `git` subprocess for local operations. Commits are stamped with a bot `GitIdentity` via per-call `-c user.name=/user.email=` (never touches global git config). Workspaces live under `{workspaces_dir}/{repo_key}/`, separate from any `local_path` reference checkout the user may have.
- `message_bus/sqlite.py` — durable SQLite-backed bus (production default). `message_bus/memory.py` — in-memory fallback for tests.
- `secrets/env.py` — reads from the process env.

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
- `web/app.py` — FastAPI app with `/` (task list), `/tasks/{id}` (detail with plans + MRs), `/plans`, `/mrs`, `/kill`, `/healthz`.
- `web/templates/` — Jinja2 templates.
- `cli/main.py` — typer commands: `db init`, `run`, `poll-once`, `plan-task`, `dev-task`.

### `runtime/`
Scheduler lives inside the FastAPI `lifespan` hook. Tasks run in the background in the same event loop as the web server: the orchestrator poll loop, the Analyst agent runner, and one Dev-agent runner per (repo, specialisation) with `backend=True` in `repositories.yaml`. A separate worker process may appear later if long-running shell sessions become a problem.

- `runtime/workers/agent_runner.py` — generic subscribe-and-dispatch loop for one agent key.
- `runtime/workers/analyst_inbox.py` — `task.discovered` handler. Transitions the Jira ticket to *In Progress*, runs AnalystAgent, comments the plan, and publishes `plan.ready` when the plan is READY and has a target repo.
- `runtime/workers/dev_inbox.py` — `plan.ready` handler per Dev-agent. On `MR_OPENED`: transitions to *Review* and comments the MR link. On `FAILED` / `NO_CHANGES`: comments the failure notes.

## Agents

| Agent | Phase | Role |
|---|---|---|
| Orchestrator | 0 | Polls Jira, persists tasks, publishes `task.discovered` |
| Analyst | 1 | Reads ticket + Confluence + MM threads, builds a plan, publishes `plan.ready` |
| Researcher | 1 | In-process MCP toolkit (code grep / KB search) used by the Analyst |
| Communicator | 1 | Read-only MM access + injection filtering; no sends yet (Phase 3 flips writes on) |
| Dev (×N) | 2 | One per (repo, specialisation); consumes `plan.ready`, writes code, opens draft MR |
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

## Data flow (Phase 2)

```
[Jira]
   │ poll
   ▼
Orchestrator ── upsert ──► tasks table
   │
   │ publish "task.discovered" → dev: analyst
   ▼
AnalystInbox
   │ (Jira: To Do → In Progress)
   │ AnalystAgent:
   │   - fetch Confluence / MM threads (read-only)
   │   - wrap via InjectionFilter
   │   - Claude Agent SDK: researcher tools + submit_plan tool
   ▼
plans table
   │ (Jira: comment plan summary)
   │
   │ if plan.status == READY and target_repo:
   │   publish "plan.ready" → dev: dev-<repo>-backend
   ▼
DevInbox
   │ gate: task.dor_satisfied == True (human-set via dashboard)
   │ DevAgent:
   │   - vcs.ensure_clone(repo)
   │   - vcs.create_branch("ai-dev/<id>-<slug>", base=default_branch)
   │   - Claude Agent SDK in cwd=workspace with
   │     Read/Glob/Grep/Edit/Write/Bash + submit_mr tool
   │   - vcs.commit_all (bot identity)
   │   - vcs.push
   │   - vcs.create_merge_request (draft=True)
   ▼
merge_requests table
   │ (Jira: In Progress → Review, comment MR link)
   ▼
[GitLab]   &   [dashboard]
```

Writes in Phase 2: Jira transition + comment, GitLab branch push + draft MR creation. **Still no MM writes** — Phase 3 flips the Communicator from read-only to write.

## Safety rails

- Every message originating outside the bot is marked `trusted=False`. LLM-facing code runs untrusted data through `InjectionFilter` (Phase 1+). Untrusted content is wrapped in `<untrusted_content>` blocks; the system prompt instructs the model to treat them as data.
- Repositories are gated through `config/repositories.yaml`: no allowlist hit → no Dev-agent.
- **Entry gate via Jira label.** Only tickets matching the configured JQL (default: `labels = "ai-dev"`) reach the orchestrator at all. Tagging a ticket in Jira is the sole "yes, take this one" signal from the operator; the rest of the pipeline is automatic. Un-tag (or remove the ticket from JQL scope) to stop future polls from rediscovering it.
- **Exit gate via MR review.** MRs are opened as draft and humans merge them. The bot never pushes to default branches.
- **Workspace isolation.** The Dev-agent writes to `{workspaces_dir}/{repo_key}/` — a bot-owned clone, never the user's hand-edited working copy.
- **Bot identity on commits.** Commits are authored by `Virtual Dev <dev_git_author_email>` (per-call `-c user.name=/user.email=`, no global git config mutation). Push uses the user's GitLab token, but the commit author makes it obvious the code came from the bot.
- **Draft MRs.** The Dev-agent opens MRs as draft by default so CI runs but humans see the WIP marker.
- No billing caps — the project runs on a Claude Max subscription, not on API credits. The only loop guard is `max_iterations_per_task` in `config/agents.yaml`, which limits the number of agent turns (protection against runaway loops, not against spend).
- `cost_usd` in the `plans` table is an informational estimate returned by `claude-agent-sdk`; nothing is enforced against it.
- Kill-switch: `POST /kill` stops the orchestrator, the Analyst runner, and every Dev-agent runner.

## Testing strategy

- `tests/unit/` — domain models, services, agents with port fakes, adapters that can be exercised without network (SQLite bus, injection filter, researcher grep over a real tmp-path git repo).
- `tests/integration/` — reserved for adapters that need live services.
- `tests/e2e/` — reserved for `virtual-dev run` against a full local fixture.

Phase 1 suite: 44 unit tests. `claude-agent-sdk` calls are isolated behind `AnalystAgent._call_model`, which tests subclass and replace with canned submissions; real subprocess invocations of `claude` are never triggered in `uv run pytest`.
