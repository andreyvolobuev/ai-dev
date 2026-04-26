"""Task-driven clarification subsystem (Phase 4.6).

ISSUE → ClarificationTask (question + info_source) → one
ClarificationAgent reasons continuously, chains tools, sends DMs to
humans, eventually calls submit_final_answer / escalate_to_lead /
abandon. The orchestrator is a thin shell that persists state,
coalesces fragments, and re-invokes the agent on human replies.

Replaces the multi-agent picker+validator pipeline of Phase 4.5 with
a Claude-Code-style single-agent loop. The agent's MCP tools are
defined inside the agent itself (because they need closures over the
running task + effects buffer), so there's no separate Tool registry
in this phase.
"""

from virtual_dev.application.services.clarification.task_orchestrator import (
    TaskOrchestrator,
    TaskOrchestratorStats,
)
from virtual_dev.application.services.clarification.task_repo import (
    ClarificationTaskRepository,
)

__all__ = [
    "ClarificationTaskRepository",
    "TaskOrchestrator",
    "TaskOrchestratorStats",
]
