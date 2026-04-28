"""Shared dependency bag passed to every tool's ``build(ctx)``.

Tools take what they need and ignore the rest. Optional services are
typed ``| None`` so a tool with a missing dep can short-circuit by
returning ``None`` from ``build()`` (the loader skips it).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from virtual_dev.application.services import (
        CommunicatorService,
        ResearcherToolkit,
    )
    from virtual_dev.application.services.agent_effects import AnalystEffect
    from virtual_dev.domain.ports.chat import ChatPort
    from virtual_dev.infrastructure.config import Settings


@dataclass
class ToolContext:
    """Dependencies + per-run state every tool can read.

    * **Long-lived** services (``communicator``, ``researcher``,
      ``chat``, ``settings``) are shared across the process; tools may
      keep them as plain attributes.
    * **Per-run** buckets (``effects``, ``submit_capture``,
      ``run_state``) are mutated by tools to record their side-effects.
      They're ``None`` outside an agent run; tools that need them must
      return ``None`` from ``build()`` when missing.

    ``submit_capture`` is shared across the terminal-submit tools of
    every agent (``submit_plan`` for analyst, ``submit_mr`` for dev,
    ``submit_response`` for responder). Each agent only ever exposes
    its own group, so there's no in-run collision — and the agent
    reads the captured args back after ``code_agent.run_task``
    returns. Was previously named ``plan_capture``; renamed when the
    other agents were folded into ``tools/`` for symmetry.
    """

    communicator: CommunicatorService | None = None
    researcher: ResearcherToolkit | None = None
    # Raw chat handle for tools that need read access bypassing the
    # communicator's rate-limit / working-hours wrapper (e.g.
    # ``read_mattermost_thread``).
    chat: ChatPort | None = None
    # Env-driven knobs for tools that talk to external systems via
    # raw HTTP (e.g. attachment downloads need ``settings.jira_token``).
    settings: Settings | None = None
    effects: list[AnalystEffect] | None = None
    submit_capture: dict[str, Any] | None = None
    run_state: dict[str, Any] | None = None
    extras: dict[str, Any] = field(default_factory=dict)
