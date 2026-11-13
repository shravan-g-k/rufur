"""Runtime configuration: paths, port, model roles and retrieval thresholds.

Values come from `~/.rufur/config.toml` (or `$RUFUR_HOME/config.toml`) layered
over the defaults below. Environment variables win for the few settings that
are handy to flip per process (home dir, port, offline mode).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ProviderRef:
    """One hop in a role's provider chain."""

    provider: str  # "ollama" | "openai_compat"
    model: str
    base_url: str | None = None
    api_key_env: str | None = None


def _chain(*refs: tuple[str, str] | tuple[str, str, str, str]) -> list[ProviderRef]:
    return [ProviderRef(*r) for r in refs]


GROQ = ("https://api.groq.com/openai/v1", "GROQ_API_KEY")


def default_roles() -> dict[str, list[ProviderRef]]:
    return {
        "extract": _chain(
            ("ollama", "qwen2.5:7b-instruct"),
            ("ollama", "gpt-oss:120b-cloud"),
            ("openai_compat", "llama-3.3-70b-versatile", *GROQ),
        ),
        "classify": _chain(
            ("ollama", "qwen2.5:7b-instruct"),
            ("ollama", "gpt-oss:120b-cloud"),
            ("openai_compat", "llama-3.3-70b-versatile", *GROQ),
        ),
        "consolidate": _chain(
            ("ollama", "qwen2.5:7b-instruct"),
            ("ollama", "gpt-oss:120b-cloud"),
            ("openai_compat", "llama-3.3-70b-versatile", *GROQ),
        ),
        "resolve": _chain(
            ("ollama", "qwen2.5:7b-instruct"),
            ("ollama", "gpt-oss:120b-cloud"),
            ("openai_compat", "llama-3.3-70b-versatile", *GROQ),
        ),
        "review": _chain(
            ("ollama", "qwen2.5:7b-instruct"),
            ("ollama", "gpt-oss:120b-cloud"),
            ("openai_compat", "llama-3.3-70b-versatile", *GROQ),
        ),
        "verify": _chain(
            ("ollama", "qwen2.5:7b-instruct"),
            ("ollama", "gpt-oss:120b-cloud"),
            ("openai_compat", "llama-3.3-70b-versatile", *GROQ),
        ),
        "forget": _chain(
            ("ollama", "qwen2.5:7b-instruct"),
            ("ollama", "gpt-oss:120b-cloud"),
            ("openai_compat", "llama-3.3-70b-versatile", *GROQ),
        ),
        "probe": _chain(
            ("ollama", "qwen3.5:4b"),
            ("ollama", "qwen2.5:7b-instruct"),
        ),
        "answer": _chain(
            ("ollama", "gemma4:12b"),
            ("ollama", "gpt-oss:120b-cloud"),
            ("openai_compat", "llama-3.3-70b-versatile", *GROQ),
        ),
    }


@dataclass
class Config:
    home: Path
    host: str = "127.0.0.1"
    port: int = 7437
    require_auth: bool = False
    default_pool: str = "default"

    ollama_url: str = "http://127.0.0.1:11434"
    embed_provider: str = "ollama"  # "ollama" | "hash" (offline, deterministic)
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768
    llm_timeout_s: float = 120.0
    offline: bool = False  # never call an LLM; extraction stays queued
    roles: dict[str, list[ProviderRef]] = field(default_factory=default_roles)

    # Write path
    checkpoint_every: int = 6  # salient unextracted events before a checkpoint extraction
    extract_idle_s: float = 30.0  # a session quiet this long gets its pending events extracted
    chunk_chars: int = 400  # extraction chunk size; small models read a sentence or two reliably
    extract_batch_chars: int = 900  # chunks per LLM call
    extract_parallel: int = 1  # concurrent extraction calls; raise with OLLAMA_NUM_PARALLEL or for cloud hops

    # Read path
    default_token_budget: int = 800
    abstain_threshold: float = 0.42  # used when the verifier LLM is unavailable
    verify_band: tuple[float, float] = (0.2, 0.6)  # relevance range where the LLM double-checks
    min_trust: str = "medium_low"
    recency_half_life_days: float = 30.0
    candidate_k: int = 40
    # Cross-encoder rerankers (read/rerank.py): "none" | "minilm" | "qwen"
    fast_reranker: str = "minilm"  # every recall; MiniLM adds ~10-30 ms
    deep_reranker: str = "qwen"  # forget-by-query: find everything the request covers
    rerank_top_n: int = 20  # fast-path candidates rescored
    minilm_reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    qwen_reranker_model: str = "Qwen/Qwen3-Reranker-0.6B"
    forget_rerank_floor: float = 0.05  # Qwen P(yes) below this is not a forget candidate (kept low: recall first)
    forget_exhaustive_max: int = 200  # up to this many visible memories, Qwen scores every one, not just search hits
    forget_judge_max: int = 30  # candidates handed to the LLM that decides what the request covers

    # Forgetting
    archive_below: float = 0.15
    retention_days: int = 90

    # Short-term memory (user-scoped, shared by all of a user's agents)
    default_user: str = "me"
    stm_base_ttl_h: float = 72.0  # lifetime from creation
    stm_extend_h: float = 24.0  # each use keeps it alive at least this long from the use
    stm_max_ttl_h: float = 168.0  # hard cap from creation
    stm_grace_h: float = 24.0  # archived (hidden, recoverable) before hard delete
    sweep_every_min: float = 15.0
    session_idle_extract_min: float = 10.0  # extract a session's messages once it has been quiet this long

    # Consolidation (STM → LTM)
    nightly_hour: int = 3  # local time
    catch_up_after_h: float = 20.0  # run at daemon start if the last run is older than this
    size_trigger_items: int = 200
    size_trigger_events: int = 300
    consolidate_batch: int = 25

    # Working set and auto-recall
    working_set_budget: int = 600
    prompt_recall_budget: int = 300

    # Subagent scratch pools
    scratch_ttl_min: int = 120

    # Conflict resolution: memory layer first, then the calling agent, then a safe default
    auto_resolve_confidence: float = 0.8  # resolver LLM must be at least this sure to settle a conflict itself
    decision_default_after_days: float = 7.0  # open decisions nobody answers get the default rule
    user_presence_window_min: float = 30.0  # decided_by="user" needs a user message this recent

    # Write-anomaly guard
    max_writes_per_min: int = 60
    max_override_attempts_per_h: int = 5

    @property
    def db_path(self) -> Path:
        return self.home / "rufur.db"

    @property
    def files_dir(self) -> Path:
        return self.home / "files"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def pid_path(self) -> Path:
        return self.home / "daemon.pid"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def ensure_dirs(self) -> None:
        for p in (self.home, self.files_dir, self.logs_dir):
            p.mkdir(parents=True, exist_ok=True)


def _apply(cfg: Config, data: dict[str, Any]) -> None:
    for key, value in data.items():
        if key == "roles":
            for role, chain in value.items():
                cfg.roles[role] = [ProviderRef(**hop) for hop in chain]
        elif hasattr(cfg, key):
            setattr(cfg, key, value)


def load_config(home: str | Path | None = None, **overrides: Any) -> Config:
    home_path = Path(home or os.environ.get("RUFUR_HOME") or Path.home() / ".rufur").expanduser()
    cfg = Config(home=home_path)
    toml_path = home_path / "config.toml"
    if toml_path.exists():
        _apply(cfg, tomllib.loads(toml_path.read_text()))
    if port := os.environ.get("RUFUR_PORT"):
        cfg.port = int(port)
    if os.environ.get("RUFUR_OFFLINE") == "1":
        cfg.offline = True
    _apply(cfg, overrides)
    return cfg
