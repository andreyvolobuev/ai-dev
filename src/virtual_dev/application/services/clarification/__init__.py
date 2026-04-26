"""Goal-driven clarification subsystem (Phase 3.9).

ISSUE → ClarificationGoal (description + why_it_matters) → planner-LLM
chooses one next step on each tick → step recorded in append-only
history → repeat until ACHIEVED / ESCALATED / ABANDONED.

Replaces the Q-tree (Phase 3.8) which copied parent-question text on
redirect — that lost the goal. Goal-driven keeps the goal central, the
planner re-composes the question for each new recipient.
"""

from virtual_dev.application.services.clarification.goal_orchestrator import (
    GoalOrchestrator,
)
from virtual_dev.application.services.clarification.goal_repo import GoalRepository

__all__ = ["GoalOrchestrator", "GoalRepository"]
