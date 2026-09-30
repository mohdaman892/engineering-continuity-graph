"""Deterministic hashed 4-gram embeddings. Same text, same vector, everywhere."""

from __future__ import annotations

import hashlib
import math

from eng_graph.embeddings import cosine_distance
from eng_graph.models import EMBED_DIM


class TestingProvider:
    __test__ = False
    name = "testing"

    def __init__(self, dim: int = EMBED_DIM) -> None:
        self.dim = dim

    def get_embedding(self, text: str) -> list[float]:
        cleaned = " ".join((text or "").lower().split())
        padded = f"  {cleaned}  "
        if len(padded) < 4:
            padded = padded.ljust(4)
        vec = [0.0] * self.dim
        for i in range(len(padded) - 3):
            gram = padded[i : i + 4]
            digest = hashlib.sha256(gram.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [v / norm for v in vec]

    def distance(self, left: list[float], right: list[float]) -> float:
        return cosine_distance(left, right)
