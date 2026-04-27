"""Side-effect records emitted by analyst tools.

Tools live under ``virtual_dev.tools`` and append :class:`AnalystEffect`
into ``ctx.effects`` instead of mutating DB / message bus directly.
The :class:`AnalystOrchestrator` (analyst_inbox) drains the list
after the run and translates each effect into a real action.

Kept in its own module so the ``virtual_dev.tools`` package can import
it without pulling in the full analyst agent (which would otherwise
cycle: agent → tools → agent).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class AnalystEffect:
    """One side-effect a tool produced during the analyst run."""

    kind: str   # "ask_dispatched" | "plan_submitted" | "escalate" | "abandon"
    payload: dict[str, Any]


__all__ = ["AnalystEffect"]
