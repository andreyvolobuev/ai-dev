"""Fastembed-backed :class:`EmbedderPort`.

Uses ONNX-runtime under the hood — no torch dependency. The model file is
downloaded lazily on first ``embed()`` call and cached in
``~/.cache/fastembed``; subsequent starts are instant.

Default model is ``paraphrase-multilingual-MiniLM-L12-v2`` (384 dim, ~220 MB)
which handles Russian + English well enough for MR titles / descriptions.
Override via the ``model_name`` constructor arg or (for the production
container) the env var ``EMBEDDER_MODEL``.
"""

from __future__ import annotations

from collections.abc import Sequence

from loguru import logger

from virtual_dev.domain.ports.embedder import EmbedderPort


_DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
_DIMENSIONS: dict[str, int] = {
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 384,
    "sentence-transformers/paraphrase-multilingual-mpnet-base-v2": 768,
    "BAAI/bge-small-en-v1.5": 384,
}


class FastembedEmbedder(EmbedderPort):
    def __init__(self, model_name: str = _DEFAULT_MODEL) -> None:
        self._model_name = model_name
        self._model: object | None = None   # fastembed.TextEmbedding
        self._dim = _DIMENSIONS.get(model_name, 0)

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        if self._dim:
            return self._dim
        # Fall back: instantiate the model and ask.
        self._ensure_loaded()
        return self._dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        self._ensure_loaded()
        assert self._model is not None
        # TextEmbedding.embed is a generator; materialise it.
        vectors = [list(v) for v in self._model.embed(list(texts))]  # type: ignore[attr-defined]
        if vectors:
            # Always trust the real vector length: a wrong _DIMENSIONS
            # entry would otherwise never self-correct, and mr_history
            # allocates its similarity matrix from `dimension`.
            self._dim = len(vectors[0])
        return vectors

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        from fastembed import TextEmbedding

        logger.info("Loading embedding model {} (may download on first run)", self._model_name)
        self._model = TextEmbedding(model_name=self._model_name)
