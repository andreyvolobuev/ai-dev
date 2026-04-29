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


_PLAIN_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss} {level: <8} "
    "{name}:{function}:{line} - {message}"
)
_COLOR_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> "
    "<level>{level: <8}</level> "
    "<cyan>{name}:{function}:{line}</cyan> - <level>{message}</level>"
)


def configure_logging(
    level: str = "INFO",
    *,
    settings: Settings | None = None,
    sink: IO[str] | None = None,
    log_file: str | None = None,
    log_file_rotation: str | None = None,
    log_file_retention: str | None = None,
) -> None:
    """Reset loguru and install a stderr sink with secret redaction.

    ``settings`` enables value-based redaction for the configured env
    tokens; pass ``None`` to skip that pass (pattern-based redaction
    still runs). ``sink`` exists so tests can capture into a buffer.

    ``log_file`` adds a rotated file sink in parallel with stderr —
    same redactor, plain (no ANSI) format. Override via explicit args
    or fall back to ``settings.log_file`` / ``log_file_rotation`` /
    ``log_file_retention``. Empty path = no file sink.
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
        format=_COLOR_FORMAT,
        backtrace=True,
        diagnose=False,   # don't leak local vars into logs
        colorize=sink is None,   # ANSI only on real terminal sink
    )

    file_path = log_file if log_file is not None else (
        getattr(settings, "log_file", "") if settings else ""
    )
    if file_path:
        rotation = log_file_rotation or (
            getattr(settings, "log_file_rotation", "20 MB") if settings else "20 MB"
        )
        retention = log_file_retention or (
            getattr(settings, "log_file_retention", "7 days") if settings else "7 days"
        )
        logger.add(
            _redacting_file_sink(
                file_path, redactor,
                max_bytes=_parse_size(rotation),
                retention_seconds=_parse_duration(retention),
            ),
            level=level.upper(),
            format=_PLAIN_FORMAT,
            backtrace=True,
            diagnose=False,
            colorize=False,
        )


def _parse_size(spec: str) -> int:
    """Loguru-ish size spec: ``20 MB`` → bytes. Falls back to 20 MB."""
    s = (spec or "").strip().upper()
    units = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}
    for suffix, mult in sorted(units.items(), key=lambda kv: -len(kv[0])):
        if s.endswith(suffix):
            num = s[: -len(suffix)].strip()
            try:
                return int(float(num) * mult)
            except ValueError:
                break
    return 20 * 1024 * 1024


def _parse_duration(spec: str) -> float:
    """``7 days`` → seconds. Accepts days/hours/minutes/seconds.
    Falls back to 7 days."""
    s = (spec or "").strip().lower()
    units = {
        "second": 1.0, "seconds": 1.0, "sec": 1.0, "secs": 1.0, "s": 1.0,
        "minute": 60.0, "minutes": 60.0, "min": 60.0, "mins": 60.0, "m": 60.0,
        "hour": 3600.0, "hours": 3600.0, "hr": 3600.0, "hrs": 3600.0, "h": 3600.0,
        "day": 86400.0, "days": 86400.0, "d": 86400.0,
        "week": 604800.0, "weeks": 604800.0, "w": 604800.0,
    }
    for suffix, mult in sorted(units.items(), key=lambda kv: -len(kv[0])):
        if s.endswith(suffix):
            num = s[: -len(suffix)].strip()
            try:
                return float(num) * mult
            except ValueError:
                break
    return 7 * 86400.0


class _RotatingRedactingSink:
    """File sink that applies the same redactor as the stderr path.

    loguru's built-in rotation/retention only fires when you pass a
    string path directly to ``logger.add``. We need the redactor in
    front of the writer, so we manage rotation ourselves: rename to
    ``<file>.<N>`` once the active file crosses ``max_bytes``, prune
    anything older than ``retention``.
    """

    def __init__(
        self, path: str, redactor: Any, *,
        max_bytes: int, retention_seconds: float,
    ) -> None:
        self._path = path
        self._redactor = redactor
        self._max_bytes = max_bytes
        self._retention_seconds = retention_seconds
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, message: Any) -> None:
        text = self._redactor(str(message))
        try:
            from pathlib import Path
            p = Path(self._path)
            if p.exists() and p.stat().st_size + len(text) > self._max_bytes:
                self._rotate(p)
            with p.open("a", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            # Don't let logging failures bring the bot down.
            pass

    def _rotate(self, p: Any) -> None:
        import time
        from pathlib import Path
        stamp = time.strftime("%Y%m%d-%H%M%S")
        rotated = Path(f"{p}.{stamp}")
        try:
            p.rename(rotated)
        except Exception:
            return
        # Prune old rotated copies past retention.
        cutoff = time.time() - self._retention_seconds
        for old in Path(p).parent.glob(f"{p.name}.*"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except Exception:
                continue


def _redacting_file_sink(
    path: str,
    redactor: Any,
    *,
    max_bytes: int = 20 * 1024 * 1024,
    retention_seconds: float = 7 * 24 * 3600,
) -> _RotatingRedactingSink:
    return _RotatingRedactingSink(
        path, redactor,
        max_bytes=max_bytes, retention_seconds=retention_seconds,
    )
