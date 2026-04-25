"""Loader for agent system prompts (``config/prompts/<name>.md``).

System prompts are the LLM's "constitution" — long, multi-paragraph,
prone to wording iterations. Keeping them as Python string literals
makes editing painful (multi-line escaping, recompile to test). This
loader reads each prompt from a markdown file at startup-time so
operators can tune the wording in YAML/MD without touching code.

Files may use ``{untrusted_warning}`` as a placeholder; the caller
substitutes the canonical injection-filter notice. Other placeholders
the caller doesn't know about are left literal so a missing key
shouldn't crash the run.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger


class PromptsLoader:
    def __init__(self, prompts_dir: str | Path) -> None:
        self._prompts_dir = Path(prompts_dir)
        self._cache: dict[str, str] = {}

    def load(self, name: str, fallback: str = "") -> str:
        """Return the contents of ``<prompts_dir>/<name>.md`` or ``fallback``.

        Cached after first read; the file is expected to be stable for
        the lifetime of the process. Restart to pick up edits.
        """
        if name in self._cache:
            return self._cache[name]
        path = self._prompts_dir / f"{name}.md"
        if not path.is_file():
            logger.warning(
                "PromptsLoader: prompt file missing — {} (using fallback)", path,
            )
            self._cache[name] = fallback
            return fallback
        text = path.read_text(encoding="utf-8").strip()
        self._cache[name] = text
        return text

    def render(self, name: str, fallback: str = "", **kwargs: str) -> str:
        """Like :meth:`load` but applies ``str.format`` with the given vars.

        Uses a tolerant formatter so unknown placeholders ({foo}) survive
        as literal text instead of crashing.
        """
        template = self.load(name, fallback=fallback)
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError) as exc:
            logger.warning(
                "PromptsLoader: format failed for {!r}: {} — returning raw text",
                name, exc,
            )
            return template
