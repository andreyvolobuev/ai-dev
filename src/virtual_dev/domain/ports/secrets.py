"""Port for secret retrieval (env / Vault / ...)."""

from __future__ import annotations

from abc import ABC, abstractmethod


class SecretsPort(ABC):
    """Abstraction over a secret store.

    Raises ``KeyError`` on missing secrets — fail loud, no silent defaults.
    """

    @abstractmethod
    def get(self, key: str) -> str:
        """Return the secret for ``key`` or raise ``KeyError``."""

    @abstractmethod
    def get_optional(self, key: str) -> str | None:
        """Return the secret for ``key`` or ``None`` if absent."""
