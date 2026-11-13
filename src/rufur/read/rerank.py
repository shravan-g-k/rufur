"""Cross-encoder rerankers: score (query, memory) pairs 0..1, far more precisely than vector cosine.

Two tiers, both local:
- `minilm` (cross-encoder/ms-marco-MiniLM-L-6-v2, 22M): cheap enough for every recall on the fast path.
- `qwen` (Qwen/Qwen3-Reranker-0.6B): an LLM reranker that reads query and memory together and answers
  "does this memory meet the request?". Used where missing something is costly: finding everything a
  forget request covers.

Models load lazily on first use. The `rerank` extra (`uv sync --extra rerank`) is optional: without it,
or if a model fails to load, `score` raises `RerankUnavailable` and callers fall back to their
unreranked behaviour.
"""

from __future__ import annotations

import math
import threading

from ..config import Config


class RerankUnavailable(RuntimeError):
    pass


def _device():
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class _Lazy:
    name = "base"

    def __init__(self, model: str):
        self.model_name = model
        self._model = None
        self._error: str | None = None
        self._lock = threading.Lock()

    def _load(self):
        raise NotImplementedError

    @property
    def state(self) -> str:
        """loaded | not loaded yet (models load on first use) | failed: <why>, for /v1/health."""
        return "loaded" if self._model is not None else f"failed: {self._error}" if self._error else "not loaded yet"

    def _ensure(self):
        with self._lock:
            if self._model is None and self._error is None:
                try:
                    self._load()
                except Exception as e:  # missing extra, no network for the first download, bad weights
                    self._error = f"{e.__class__.__name__}: {e}"
            if self._error:
                raise RerankUnavailable(f"{self.name} reranker unavailable: {self._error}")

    def score(self, query: str, docs: list[str], instruction: str | None = None) -> list[float]:
        raise NotImplementedError


class MiniLMReranker(_Lazy):
    name = "minilm"

    def _load(self):
        from sentence_transformers import CrossEncoder

        self._model = CrossEncoder(self.model_name, device=_device())

    def score(self, query: str, docs: list[str], instruction: str | None = None) -> list[float]:
        if not docs:
            return []
        self._ensure()
        logits = self._model.predict([(query, d) for d in docs], batch_size=32, show_progress_bar=False)
        return [1.0 / (1.0 + math.exp(-float(x))) for x in logits]


QWEN_PREFIX = (
    "<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct "
    'provided. Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
)
QWEN_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
QWEN_DEFAULT_INSTRUCTION = "Given a question, judge whether this stored memory answers it"


class QwenReranker(_Lazy):
    """Qwen3-Reranker scores P("yes") on the official prompt template; other wording loses accuracy."""

    name = "qwen"
    batch = 8
    max_len = 1024

    def _load(self):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self._torch = torch
        self._dev = _device()
        self._tok = AutoTokenizer.from_pretrained(self.model_name, padding_side="left")
        dtype = torch.float16 if self._dev != "cpu" else torch.float32
        self._model = AutoModelForCausalLM.from_pretrained(self.model_name, dtype=dtype).to(self._dev).eval()
        self._yes = self._tok.convert_tokens_to_ids("yes")
        self._no = self._tok.convert_tokens_to_ids("no")

    def score(self, query: str, docs: list[str], instruction: str | None = None) -> list[float]:
        if not docs:
            return []
        self._ensure()
        torch, tok = self._torch, self._tok
        inst = instruction or QWEN_DEFAULT_INSTRUCTION
        out: list[float] = []
        for i in range(0, len(docs), self.batch):
            texts = [f"{QWEN_PREFIX}<Instruct>: {inst}\n<Query>: {query}\n<Document>: {d}{QWEN_SUFFIX}"
                     for d in docs[i : i + self.batch]]  # fmt: skip
            enc = tok(texts, padding=True, truncation=True, max_length=self.max_len, return_tensors="pt").to(self._dev)
            with torch.no_grad():
                last = self._model(**enc).logits[:, -1, :]
            pair = torch.stack([last[:, self._no], last[:, self._yes]], dim=1).float()
            out.extend(torch.softmax(pair, dim=1)[:, 1].tolist())
        return out


def make_reranker(cfg: Config, which: str):
    """`which`: "none" | "minilm" | "qwen". Returns None for "none"."""
    if which == "minilm":
        return MiniLMReranker(cfg.minilm_reranker_model)
    if which == "qwen":
        return QwenReranker(cfg.qwen_reranker_model)
    return None
