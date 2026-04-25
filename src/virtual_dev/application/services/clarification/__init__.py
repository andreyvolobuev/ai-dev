"""Clarification subsystem (Phase 3.8).

ISSUE → QUESTION (with STAKEHOLDER + REASONING) → ANSWER. Multi-fragment
DM replies are coalesced after a silence window, classified by an LLM,
and either close the question or spawn a child (REDIRECT,
COUNTER_QUESTION, ASKING_FOR_STAKEHOLDER).

The orchestrator owns the state machine; the repo is the only thing
that touches DB rows; the coalescer / classifier / counter-answerer
are stateless services driven by orchestrator ticks.
"""

from virtual_dev.application.services.clarification.orchestrator import (
    ClarificationOrchestrator,
)
from virtual_dev.application.services.clarification.repo import QuestionRepository
from virtual_dev.application.services.clarification.stakeholder_resolver import (
    StakeholderResolver,
)

__all__ = [
    "ClarificationOrchestrator",
    "QuestionRepository",
    "StakeholderResolver",
]
