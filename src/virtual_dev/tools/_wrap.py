"""Tiny helper used by every tool to wrap a JSON-able payload into the
MCP ``content``-block shape Claude expects."""

from __future__ import annotations

import json
from typing import Any


def wrap_text(payload: dict[str, Any]) -> dict[str, Any]:
    """JSON-encode ``payload`` and stuff it into one MCP text block.
    Use ``ensure_ascii=False`` so Russian text stays readable in the
    activity feed."""
    return {"content": [{
        "type": "text",
        "text": json.dumps(payload, ensure_ascii=False),
    }]}
