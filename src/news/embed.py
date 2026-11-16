"""Sentence embeddings, computed locally (no API cost) with fastembed's ONNX
build of BAAI/bge-small-en-v1.5 (384 dimensions). Vectors are normalised, so
cosine similarity is a dot product.

Passages (items, stories) are embedded as they are; queries (search phrases,
topic descriptions) get bge's retrieval prefix. NEWS_FAKE_EMBED=1 swaps in a
deterministic hashed bag-of-words embedder for tests, so they need neither
the model download nor the network."""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import threading
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from news.db.models import DIM

MODEL = "BAAI/bge-small-en-v1.5"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

_lock = threading.Lock()
_WORD = re.compile(r"\w+")


def _fake(texts: Sequence[str]) -> list[list[float]]:
    out = []
    for text in texts:
        v = [0.0] * DIM
        words = _WORD.findall(text.removeprefix(QUERY_PREFIX).lower())
        for w in words + [f"{a} {b}" for a, b in zip(words, words[1:], strict=False)]:
            h = int.from_bytes(hashlib.blake2b(w.encode(), digest_size=4).digest(), "big")
            v[h % DIM] += 1.0 if h & 1 << 31 else -1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / norm for x in v])
    return out


@lru_cache
def _model() -> Any:
    from fastembed import TextEmbedding

    return TextEmbedding(MODEL, cache_dir=os.environ.get("NEWS_MODEL_DIR", "/models"), threads=2)


def _embed(texts: Sequence[str]) -> list[list[float]]:
    if not texts:
        return []
    if os.environ.get("NEWS_FAKE_EMBED") == "1":
        return _fake(texts)
    with _lock:  # the ONNX session is shared; keep one batch at a time
        return [[float(x) for x in v] for v in _model().embed(list(texts), batch_size=32)]


async def embed_passages(texts: Sequence[str]) -> list[list[float]]:
    return await asyncio.to_thread(_embed, [t[:2000] for t in texts])


async def embed_queries(texts: Sequence[str]) -> list[list[float]]:
    return await asyncio.to_thread(_embed, [QUERY_PREFIX + t[:500] for t in texts])


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return float(sum(x * y for x, y in zip(a, b, strict=True)))


def normalise(v: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def vec(v: Sequence[float]) -> str:
    """pgvector text literal, for raw SQL with CAST(:v AS vector)."""
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def item_text(title: str, snippet: str) -> str:
    return f"{title}. {snippet}" if snippet else title
