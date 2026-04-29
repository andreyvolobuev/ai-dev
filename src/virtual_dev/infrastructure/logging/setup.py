"""Loguru configuration with secret redaction.

Every log line passes through :func:`_make_redactor` before reaching a
sink. Three patterns + a value-blacklist:

* ``Bearer <tok>`` and ``Authorization: Bearer <tok>``
* ``https://user:tok@host`` (the form `git push` errors quote)
* ``key=value`` for known credential keys (``token``, ``api_key``,
  ``password``, ``secret``)
* Exact-match against the configured ``Settings`` env tokens
  (``GITLAB_TOKEN``, ``JIRA_TOKEN``, etc.) — catches plain-text logs of
  tokens that didn't use any of the patterns above.
"""

from __future__ import annotations

import contextlib
import re
import sys
from collections.abc import Callable
from typing import IO, Any

from loguru import logger

from virtual_dev.infrastructure.config import Settings

_REDACTED = "[REDACTED]"

# Bearer / oauth-style headers. Token = any non-whitespace run after
# "Bearer ". Case-insensitive on the keyword only.
_BEARER_RE = re.compile(r"\b([Bb]earer)\s+\S+")
# https://user:pass@host — credentials in the URL userinfo section.
_URL_CREDS_RE = re.compile(
    r"(https?://)([^:/@\s]+):([^/@\s]+)@",
)
# key=value for common credential keys. Stops at whitespace, `&`, `,`,
# closing quote or paren — covers query strings, env-style logs, and
# mostly-shell-quoted output.
_KV_RE = re.compile(
    r"\b(token|api[_-]?key|password|passwd|pwd|secret)\s*=\s*([^\s&,\"')]+)",
    flags=re.IGNORECASE,
)

# Settings attributes that hold raw secrets. We redact each non-empty
# value verbatim. Anything < this length is too generic to be safe to
# treat as a secret (would risk masking unrelated text).
_MIN_SECRET_LEN = 6
_SECRET_FIELDS: tuple[str, ...] = (
    "gitlab_token",
    "jira_token",
    "mattermost_token",
    "confluence_token",
    "admin_token",
)


def _make_redactor(settings: Settings | None) -> Callable[[str], str]:
    """Build a closure that redacts secrets in a single text blob.

    The closure is applied to fully-formatted log records (after loguru
    interpolates ``{...}`` placeholders), so it sees the same string
    that would have hit the sink.
    """
    raw_secrets: list[str] = []
    if settings is not None:
        for field in _SECRET_FIELDS:
            value = (getattr(settings, field, "") or "").strip()
            if value and len(value) >= _MIN_SECRET_LEN:
                raw_secrets.append(value)
    # Longest-first so a token that happens to contain a shorter one
    # gets handled before the shorter substring kicks in.
    raw_secrets.sort(key=len, reverse=True)

    def redact(text: str) -> str:
        text = _BEARER_RE.sub(rf"\1 {_REDACTED}", text)
        text = _URL_CREDS_RE.sub(rf"\1\2:{_REDACTED}@", text)
        text = _KV_RE.sub(rf"\1={_REDACTED}", text)
        for value in raw_secrets:
            if value in text:
                text = text.replace(value, _REDACTED)
        return text

    return redact


def configure_logging(
    level: str = "INFO",
    *,
    settings: Settings | None = None,
    sink: IO[str] | None = None,
) -> None:
    """Reset loguru and install a stderr sink with secret redaction.

    ``settings`` enables value-based redaction for the configured env
    tokens; pass ``None`` to skip that pass (pattern-based redaction
    still runs). ``sink`` exists so tests can capture into a buffer.
    """
    logger.remove()
    target: IO[str] = sink if sink is not None else sys.stderr
    redactor = _make_redactor(settings)

    def write(message: Any) -> None:
        # ``message`` is loguru's ``Message`` proxy; str() yields the
        # final formatted+coloured line including the trailing "\n".
        target.write(redactor(str(message)))
        with contextlib.suppress(Exception):
            target.flush()

    logger.add(
        write,
        level=level.upper(),
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> "
            "<level>{level: <8}</level> "
            "<cyan>{name}:{function}:{line}</cyan> - <level>{message}</level>"
        ),
        backtrace=True,
        diagnose=False,   # don't leak local vars into logs
        colorize=sink is None,   # ANSI only on real terminal sink
    )
