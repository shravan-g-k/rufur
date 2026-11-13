"""Text embeddings: Ollama `nomic-embed-text` by default, hashing fallback offline.

`HashEmbedder` is a deterministic bag-of-words projection used by tests and
fully-offline installs. Vectors from different embedders are never mixed in one
index: if Ollama is down, `embed` raises `EmbedUnavailable`, the write path
skips the vector row, and `MemoryService.reconcile_embeddings` fills it in later.
"""

from __future__ import annotations

import hashlib
import math
import re
import time

import httpx

from .config import Config


class EmbedUnavailable(RuntimeError):
    pass


_WORD = re.compile(r"[a-z0-9]+")


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class HashEmbedder:
    name = "hash"
    # Cosine range that maps to relevance 0..1 in the read path (see read/recall.py).
    cos_floor, cos_span = 0.05, 0.5

    def __init__(self, dim: int):
        self.dim = dim

    def embed(self, texts: list[str], kind: str = "document") -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            words = _WORD.findall(text.lower())
            grams = words + [f"{a}_{b}" for a, b in zip(words, words[1:])]
            for g in grams:
                h = int.from_bytes(hashlib.blake2b(g.encode(), digest_size=8).digest(), "little")
                vec[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
            out.append(_normalize(vec))
        return out


class OllamaEmbedder:
    cos_floor, cos_span = 0.45, 0.35

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.name = f"ollama:{cfg.embed_model}"
        self.client = httpx.Client(timeout=60)
        self._down_until = 0.0

    def embed(self, texts: list[str], kind: str = "document") -> list[list[float]]:
        if not texts:
            return []
        if time.time() < self._down_until:
            raise EmbedUnavailable("ollama embedder recently unreachable")
        # nomic-embed-text is trained with task prefixes.
        prefix = "search_query: " if kind == "query" else "search_document: "
        try:
            r = self.client.post(
                f"{self.cfg.ollama_url}/api/embed",
                json={"model": self.cfg.embed_model, "input": [prefix + t for t in texts], "keep_alive": "30m"},
            )
            r.raise_for_status()
            return [_normalize(v) for v in r.json()["embeddings"]]
        except (httpx.HTTPError, KeyError) as e:
            self._down_until = time.time() + 30
            raise EmbedUnavailable(str(e)) from e


def make_embedder(cfg: Config):
    if cfg.embed_provider == "hash" or cfg.offline:
        return HashEmbedder(cfg.embed_dim)
    return OllamaEmbedder(cfg)
