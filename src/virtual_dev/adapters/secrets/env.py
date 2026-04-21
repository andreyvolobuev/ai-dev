"""Env-based secrets adapter. Phase 0 default; replace with Vault later."""

from __future__ import annotations

import os

from virtual_dev.domain.ports.secrets import SecretsPort


class EnvSecrets(SecretsPort):
    """Reads secrets from the process environment.

    The ``.env`` file is loaded by pydantic-settings in :mod:`Settings`, so by
    the time this adapter is instantiated, everything in ``.env`` is already
    in ``os.environ``.
    """

    def get(self, key: str) -> str:
        value = os.environ.get(key)
        if value is None or value == "":
            raise KeyError(f"Secret '{key}' is not set")
        return value

    def get_optional(self, key: str) -> str | None:
        value = os.environ.get(key)
        if value is None or value == "":
            return None
        return value
