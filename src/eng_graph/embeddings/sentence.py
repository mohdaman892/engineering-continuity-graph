"""Optional offline neural providers.

Both providers set ``HF_HUB_OFFLINE=1`` before touching a model. If the weights
are not already on disk, they raise ``ModelUnavailable`` instead of downloading.
"""

from __future__ import annotations

import os

from eng_graph.embeddings import ModelUnavailable, cosine_distance


def _force_offline() -> None:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"


def _normalize(vec: list[float]) -> list[float]:
    norm = sum(v * v for v in vec) ** 0.5
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


class SentenceProvider:
    """``all-MiniLM-L6-v2`` through sentence-transformers, local files only."""

    name = "sentence-transformers"

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        _force_offline()
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ModelUnavailable(
                "sentence-transformers is not installed; refusing to fetch a model"
            ) from exc
        try:
            self._model = SentenceTransformer(model_name, local_files_only=True)
        except TypeError:
            try:
                self._model = SentenceTransformer(model_name)
            except Exception as exc:
                raise ModelUnavailable(
                    f"model {model_name} is not cached locally; download refused"
                ) from exc
        except Exception as exc:
            raise ModelUnavailable(
                f"model {model_name} is not cached locally; download refused"
            ) from exc
        self._model_name = model_name

    def get_embedding(self, text: str) -> list[float]:
        vec = self._model.encode(text or "", normalize_embeddings=True)
        return [float(v) for v in vec]

    def distance(self, left: list[float], right: list[float]) -> float:
        return cosine_distance(left, right)


class OfflineTransformersProvider:
    """Offline transformers encoder. Rejects a cache miss instead of downloading."""

    name = "openai-transformers"

    def __init__(self, model_name: str | None = None) -> None:
        _force_offline()
        self._model_name = model_name or os.environ.get(
            "ECG_OFFLINE_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
        )
        try:
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:
            raise ModelUnavailable(
                "transformers is not installed; refusing to fetch a model"
            ) from exc
        try:
            self._tokenizer = AutoTokenizer.from_pretrained(
                self._model_name, local_files_only=True
            )
            self._model = AutoModel.from_pretrained(self._model_name, local_files_only=True)
        except Exception as exc:
            raise ModelUnavailable(
                f"model {self._model_name} is not cached locally; download refused"
            ) from exc
        self._torch = __import__("torch")

    def get_embedding(self, text: str) -> list[float]:
        torch = self._torch
        encoded = self._tokenizer(
            text or "",
            return_tensors="pt",
            truncation=True,
            max_length=256,
        )
        with torch.no_grad():
            output = self._model(**encoded)
            token_vecs = output.last_hidden_state
            mask = encoded["attention_mask"].unsqueeze(-1)
            summed = (token_vecs * mask).sum(dim=1)
            counts = mask.sum(dim=1).clamp(min=1)
            pooled = (summed / counts).squeeze(0)
        return _normalize([float(v) for v in pooled.tolist()])

    def distance(self, left: list[float], right: list[float]) -> float:
        return cosine_distance(left, right)
