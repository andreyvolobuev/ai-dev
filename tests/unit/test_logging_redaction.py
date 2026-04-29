"""Logging redaction tests.

Anything that smells like a secret must NOT survive the formatter:
* ``Authorization: Bearer <tok>`` headers
* ``https://user:tok@host/...`` embedded creds (the form ``git push``
  errors quote in their messages)
* ``password=…`` / ``token=…`` / ``api_key=…`` query-style args
* Known env-secret values (so even a plain ``logger.info("hello {}",
  os.environ['JIRA_TOKEN'])`` doesn't leak)
"""

from __future__ import annotations

import io
import os
from unittest import mock

from loguru import logger

from virtual_dev.infrastructure.config import Settings
from virtual_dev.infrastructure.logging import configure_logging


def _capture(settings: Settings | None = None) -> io.StringIO:
    """Reconfigure loguru to write into an in-memory buffer + return it.
    Tests inspect the buffer's contents after logging."""
    buffer = io.StringIO()
    configure_logging("DEBUG", settings=settings, sink=buffer)
    return buffer


def _restore_default_logging() -> None:
    """Reset to default stderr so other tests aren't affected."""
    configure_logging("INFO")


def test_bearer_header_is_redacted() -> None:
    buf = _capture()
    try:
        logger.error("auth failed: Authorization: Bearer abc123XYZsecret")
    finally:
        _restore_default_logging()

    out = buf.getvalue()
    assert "abc123XYZsecret" not in out
    assert "Bearer" in out and "[REDACTED]" in out


def test_url_with_embedded_credentials_is_redacted() -> None:
    """``git push`` errors quote the remote URL with the inlined PAT —
    the most common leak path on the bot."""
    buf = _capture()
    try:
        logger.warning(
            "git push failed: fatal: unable to access "
            "'https://oauth2:secrettoken123@gitlab.example.com/r.git'"
        )
    finally:
        _restore_default_logging()

    out = buf.getvalue()
    assert "secrettoken123" not in out
    # Host preserved so the operator can still tell which remote failed.
    assert "gitlab.example.com" in out


def test_password_query_param_is_redacted() -> None:
    buf = _capture()
    try:
        logger.info("config: api_key=SECRET_KEY_42 token=DEADBEEF")
    finally:
        _restore_default_logging()

    out = buf.getvalue()
    assert "SECRET_KEY_42" not in out
    assert "DEADBEEF" not in out


def test_known_env_secrets_are_redacted_even_in_plain_text() -> None:
    """A bug somewhere logs a token without any nearby key/header. The
    redactor still catches it because the value matches a configured
    env secret."""
    fake_env = {
        "JIRA_TOKEN": "jira-token-uniqueABC",
        "GITLAB_TOKEN": "gl-tok-XYZdef",
        "MATTERMOST_TOKEN": "",  # empty — must not redact empty strings!
        "CONFLUENCE_TOKEN": "",
        "ADMIN_TOKEN": "",
    }
    with mock.patch.dict(os.environ, fake_env, clear=False):
        settings = Settings()  # picks up fake env

    buf = _capture(settings=settings)
    try:
        logger.info("oops we logged jira-token-uniqueABC plainly")
        logger.info("and then gl-tok-XYZdef somewhere else")
        # Empty strings must NOT cause global ""→[REDACTED] catastrophe.
        logger.info("normal log line with no secret")
    finally:
        _restore_default_logging()

    out = buf.getvalue()
    assert "jira-token-uniqueABC" not in out
    assert "gl-tok-XYZdef" not in out
    # Sanity: a normal log line is unchanged in spirit.
    assert "normal log line" in out


def test_redaction_does_not_mangle_normal_lines() -> None:
    buf = _capture()
    try:
        logger.info("starting orchestrator with poll_interval=60s")
        logger.info("MR !42 opened on bellingshausen")
    finally:
        _restore_default_logging()

    out = buf.getvalue()
    assert "starting orchestrator" in out
    assert "MR !42 opened" in out
    assert "[REDACTED]" not in out  # nothing to redact
