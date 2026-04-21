"""Port for text embedding.

Kept as its own port so tests can substitute a deterministic fake without
pulling onnxruntime / fastembed. The only runtime dependency of the real
adapter is the embedding model itself, loaded lazily on first call and
cached in ``~/.cache/fastembed``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence


class EmbedderPort(ABC):
    @abstractmethod
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of texts and return one vector per input text."""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Dimensionality of the returned vectors. Stable across calls."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Identifier of the model used (e.g. for index invalidation)."""
