"""Text similarity: tokens, Jaccard, character trigrams, TF-IDF cosine (pure Python).

Embeddings are optional. ``NullEmbedder`` is the default, so the pipeline is fully
reproducible without any model download. ``SentenceTransformerEmbedder`` is used
only if the package is installed AND enabled in configuration; its model name is
then recorded in ``model_version``.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter
from typing import Protocol

from app.pipeline.text import STOPWORDS

_STOP = {w for words in STOPWORDS.values() for w in words} | {"s", "t", "new", "says", "nouveau", "nouvelle"}


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", fold(text)) if (len(t) > 1 or t.isdigit()) and t not in _STOP]


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def trigrams(text: str) -> set[str]:
    s = " ".join(tokens(text))
    return {s[i:i + 3] for i in range(max(0, len(s) - 2))}


def title_similarity(a: str, b: str) -> tuple[float, float]:
    return jaccard(set(tokens(a)), set(tokens(b))), jaccard(trigrams(a), trigrams(b))


def content_hash(text: str) -> str:
    return hashlib.sha1(" ".join(tokens(text)).encode()).hexdigest()


class TfIdf:
    """Sparse TF-IDF vectors over one corpus (sublinear tf, smoothed idf, L2 norm)."""

    version = "tfidf-1"

    def __init__(self, docs: dict[str, str]):
        toks = {k: tokens(v) for k, v in docs.items()}
        df = Counter(t for ts in toks.values() for t in set(ts))
        n = len(docs)
        self.idf = {t: math.log((1 + n) / (1 + d)) + 1.0 for t, d in df.items()}
        self.vectors: dict[str, dict[str, float]] = {}
        for k, ts in toks.items():
            tf = Counter(ts)
            vec = {t: (1 + math.log(c)) * self.idf[t] for t, c in tf.items()}
            norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
            self.vectors[k] = {t: v / norm for t, v in vec.items()}

    def cosine(self, a: str, b: str) -> float:
        va, vb = self.vectors.get(a), self.vectors.get(b)
        if not va or not vb:
            return 0.0
        if len(va) > len(vb):
            va, vb = vb, va
        return sum(v * vb.get(t, 0.0) for t, v in va.items())


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]] | None: ...


class NullEmbedder:
    name = "none"

    def embed(self, texts: list[str]) -> None:
        return None


class SentenceTransformerEmbedder:  # pragma: no cover - optional, needs a local model
    """Optional local embeddings. Not used unless explicitly enabled."""

    def __init__(self, model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"):
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415 - optional dependency

        self._model = SentenceTransformer(model)
        self.name = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._model.encode(texts, normalize_embeddings=True)]


def make_embedder(enabled: bool = False, model: str | None = None) -> Embedder:
    if not enabled:
        return NullEmbedder()
    try:  # pragma: no cover - optional
        return SentenceTransformerEmbedder(model) if model else SentenceTransformerEmbedder()
    except Exception:  # noqa: BLE001 - model unavailable: degrade to TF-IDF only
        return NullEmbedder()
