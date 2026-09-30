"""Local embedding providers. Nothing is downloaded from here."""

from __future__ import annotations

import os
from typing import Protocol


class EmbeddingProvider(Protocol):
    name: str

    def get_embedding(self, text: str) -> list[float]:
        """Return an L2-normalized vector."""

    def distance(self, left: list[float], right: list[float]) -> float:
        """Cosine distance in ``[0, 2]`` (``0`` means identical)."""


class ModelUnavailable(RuntimeError):
    """The selected provider cannot run offline with what is installed."""


def provider_name() -> str:
    return os.environ.get("ECG_EMBEDDING_PROVIDER", "testing").strip() or "testing"


def get_provider(name: str | None = None) -> EmbeddingProvider:
    """Build the provider named by ``ECG_EMBEDDING_PROVIDER``.

    ``testing`` is the default: deterministic 4-gram vectors, no download.
    ``sentence-transformers`` and ``openai-transformers`` stay fully offline
    and fail if the model is not already cached.
    """
    chosen = (name if name is not None else provider_name()).strip().lower()
    if chosen in ("testing", "test", "fallback", "ngram"):
        from eng_graph.embeddings.testing import TestingProvider

        return TestingProvider()
    if chosen in ("sentence-transformers", "sentence", "sentencetransformer"):
        from eng_graph.embeddings.sentence import SentenceProvider

        return SentenceProvider()
    if chosen in ("openai-transformers", "openai", "transformers"):
        from eng_graph.embeddings.sentence import OfflineTransformersProvider

        return OfflineTransformersProvider()
    raise ModelUnavailable(
        f"unknown ECG_EMBEDDING_PROVIDER {chosen!r}; "
        "use 'testing', 'sentence-transformers', or 'openai-transformers'"
    )


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    for a, b in zip(left, right):
        dot += a * b
    if dot < 0.0:
        return 0.0
    if dot > 1.0:
        return 1.0
    return dot


def cosine_distance(left: list[float], right: list[float]) -> float:
    return 1.0 - cosine_similarity(left, right)
