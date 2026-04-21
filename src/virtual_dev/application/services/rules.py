"""Loader for per-agent rule files (``config/rules/<agent>.md``).

These files hold short, human-curated guidance for a specific agent key —
things like "use double quotes" or "prefer pytest.approx for floats". The
agent's system prompt gets them spliced in verbatim.

No validation of content: rules are free-form markdown and trust is
implicit (they're committed alongside the code). If a file is missing we
return ``""`` quietly so that the system prompt works even before a given
rule set has been authored.
"""

from __future__ import annotations

from pathlib import Path


class RulesLoader:
    def __init__(self, rules_dir: str | Path) -> None:
        self._rules_dir = Path(rules_dir)

    def load(self, agent_key: str) -> str:
        """Return the contents of ``<rules_dir>/<agent_key>.md`` or ``""``."""
        path = self._rules_dir / f"{agent_key}.md"
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8").strip()

    def exists(self, agent_key: str) -> bool:
        return (self._rules_dir / f"{agent_key}.md").is_file()
