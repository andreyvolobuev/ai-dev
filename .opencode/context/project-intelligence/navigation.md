<!-- Context: project-intelligence/nav | Priority: high | Version: 1.1 | Updated: 2026-07-02 -->

# Project Intelligence — Virtual Dev

> Мульти-агентный AI-разработчик для команды DataMining (2GIS).
> Полный цикл: Jira → анализ → код → MR → ревью → CI → мёрж.

## Structure

```
.opencode/context/project-intelligence/
├── navigation.md              # This file — quick overview
├── business-domain.md         # Team, stakeholders, communication rules, Jira workflow
├── technical-domain.md        # Stack, LLM-infra, architecture, agents, repos
├── business-tech-bridge.md    # How business needs map to technical solutions
├── decisions-log.md           # Key decisions with rationale (7 documented)
└── living-notes.md            # Tech debt, roadmap, gotchas, patterns
```

**Total**: ~480 lines across 6 files. Read in order for full context.

## Quick Start (must-read for AI agent)

| Priority | What | File |
|----------|------|------|
| ⚠️ CRITICAL | LLM-infra rules (no API key, no budgets!) | `technical-domain.md` → "CRITICAL: LLM-инфра" |
| 🔵 Must | Agent roster, message bus, topics | `technical-domain.md` → "Agents" |
| 🔵 Must | Business rules, working hours, esc policies | `business-domain.md` |
| 🔵 Must | OpenCode platform — subagents, commands, skills | `technical-domain.md` → "OpenCode Platform" |
| 🟢 Should | Key decisions rationale | `decisions-log.md` |
| 🟢 Should | CI handling, review gates, WS strategy | `decisions-log.md` (items 5-7) |
| 🟢 Should | Active tech debt & gotchas | `living-notes.md` |

## Quick Reference

- **Project**: Virtual Dev — Python 3.13+, uv, Claude Max, Jira/GitLab/MM/Confluence (self-hosted)
- **LLM**: Opus 4.8 (default, `claude-opus-4-8`), Haiku 4.5-20251001 (lightweight)
- **Architecture**: Hexagonal (ports & adapters), 6 agents, SQLite message bus, 19 MCP tools
- **Stage**: Phase 5.0 complete (314 tests), Phase 4 in progress (real-team rollout)
- **Key people**: Тимлид (основной контакт), DataMining team (пользователи)
- **Repo**: `bellingshausen` активно, еще 6 подключены конфигом

## Related Files
- `.opencode/context/core/standards/project-intelligence.md` — Standards for this folder
- `.opencode/context/core/context-system.md` — Broader context architecture
