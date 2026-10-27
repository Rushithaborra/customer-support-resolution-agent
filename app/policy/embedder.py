"""Text embeddings. Local BAAI/bge-small-en-v1.5 via fastembed (ONNX): free,
offline after the first ~130 MB model download, and deterministic."""

import hashlib
import math
import os
import re
from functools import lru_cache
from typing import Protocol

from app.config import settings
from app.db.models import EMBEDDING_DIM


class Embedder(Protocol):
    model_name: str
    dim: int

    def embed_query(self, text: str) -> list[float]: ...

    def embed_passages(self, texts: list[str]) -> list[list[float]]: ...


def _normalize(v) -> list[float]:
    norm = math.sqrt(sum(float(x) * float(x) for x in v)) or 1.0
    return [float(x) / norm for x in v]


class FastEmbedder:
    model_name = "BAAI/bge-small-en-v1.5"
    dim = EMBEDDING_DIM
    # BGE v1.5 is asymmetric: queries get this instruction, passages don't. fastembed's
    # query_embed does NOT add it for this model, so we do. Getting it wrong quietly hurts retrieval.
    QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "

    def __init__(self, cache_dir: str | None = None) -> None:
        from fastembed import TextEmbedding  # imported lazily: loading the model takes a moment
        self._model = TextEmbedding(self.model_name,
                                    cache_dir=os.path.expanduser(cache_dir or settings.embedding_cache_dir))

    def embed_query(self, text: str) -> list[float]:
        return _normalize(next(iter(self._model.embed([self.QUERY_INSTRUCTION + text]))))

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [_normalize(v) for v in self._model.passage_embed(texts)]


class FakeEmbedder:
    """Deterministic bag-of-words hashing. For plumbing tests only; it knows nothing about meaning."""
    model_name = "fake-hash"
    dim = EMBEDDING_DIM

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            v[int(hashlib.sha256(tok.encode()).hexdigest(), 16) % self.dim] += 1.0
        return _normalize(v)

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    """FastAPI dependency and default. One model instance per process."""
    return FastEmbedder()
