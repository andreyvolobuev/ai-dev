"""Task-driven clarification subsystem (Phase 4.5).

ISSUE → ClarificationTask (question + info_source/info_source_class)
→ Tool Picker LLM picks ONE tool per tick → tool runs (SYNC | ASYNC |
META) → Validator LLM checks chain → tasks marked solved / not →
loop until every top-level task is closed.

Replaces the goal-driven model (Phase 3.9) which had a hardcoded
state machine and 6 fixed planner actions. Now actions ARE tools, and
adding a new tool is a matter of dropping a file under
:mod:`virtual_dev.skills.tools` with a ``@tool_`` decorator.
"""

from virtual_dev.application.services.clarification.task_orchestrator import (
    TaskOrchestrator,
    TaskOrchestratorStats,
)
from virtual_dev.application.services.clarification.task_repo import (
    ClarificationTaskRepository,
)
from virtual_dev.application.services.clarification.tools import (
    PendingReply,
    Tool,
    ToolContext,
    ToolOutcome,
    ToolRegistry,
    discover_builtin_tools,
    get_tool_registry,
)

__all__ = [
    "ClarificationTaskRepository",
    "PendingReply",
    "TaskOrchestrator",
    "TaskOrchestratorStats",
    "Tool",
    "ToolContext",
    "ToolOutcome",
    "ToolRegistry",
    "discover_builtin_tools",
    "get_tool_registry",
]
