"""Role-routed structured LLM calls: local Ollama first, free cloud as fallback.

Every call asks for JSON matching a pydantic model. Output that fails to
validate is retried once on the same hop, then the next hop in the role's
chain is tried. If the whole chain fails the caller gets `LLMUnavailable` and
must fall back to a safe deterministic outcome (reject or "unclear").
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .config import Config, ProviderRef

T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(RuntimeError):
    pass


@dataclass
class LLMResult:
    data: Any
    model: str
    tokens_in: int
    tokens_out: int
    latency_ms: float


class LLM:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = httpx.Client(timeout=cfg.llm_timeout_s)
        # Hops that failed with a connection error are skipped for a while so a
        # stopped Ollama doesn't add a timeout to every call.
        self._down_until: dict[str, float] = {}

    def complete(self, role: str, system: str, user: str, schema: type[T], *, accept: Callable[[T], bool] | None = None,
                 num_predict: int = 2048) -> tuple[T, LLMResult]:  # fmt: skip
        """`accept` rejects an answer that parses but is unusable; the next model in the chain gets a go.
        If no model passes it, the first parsed answer is returned and the caller deals with the bad parts."""
        if self.cfg.offline:
            raise LLMUnavailable("offline mode")
        errors: list[str] = []
        fallback: tuple[T, LLMResult] | None = None
        for hop in self.cfg.roles.get(role, []):
            hop_key = f"{hop.provider}:{hop.base_url or self.cfg.ollama_url}"
            if self._down_until.get(hop_key, 0) > time.time():
                continue
            if hop.api_key_env and not os.environ.get(hop.api_key_env):
                continue
            for _attempt in range(2):
                try:
                    result = self._call(hop, system, user, schema, num_predict)
                    parsed = schema.model_validate(result.data)
                    if accept is None or accept(parsed):
                        return parsed, result
                    fallback = fallback or (parsed, result)
                    errors.append(f"{hop.model}: answer rejected")
                    break
                except (ValidationError, json.JSONDecodeError) as e:
                    errors.append(f"{hop.model}: invalid output ({e.__class__.__name__})")
                except httpx.ConnectError as e:
                    self._down_until[hop_key] = time.time() + 30
                    errors.append(f"{hop.model}: {e}")
                    break
                except (httpx.HTTPError, KeyError) as e:
                    errors.append(f"{hop.model}: {e}")
                    break
        if fallback:
            return fallback
        raise LLMUnavailable("; ".join(errors) or f"no usable provider for role {role!r}")

    def decide(self, state: str, questions: dict[str, dict]) -> tuple[dict[str, dict], int]:
        """Typed decisions (noul / choice / score) from a decision model such as Kev via Ollama's /v1/systemone.

        It returns calibrated probabilities with no generated text, so it is much cheaper than `complete` for
        yes/no and pick-one questions. Raises `LLMUnavailable` for any failure; callers fall back to `complete`.
        """
        if self.cfg.offline or not self.cfg.decision_model:
            raise LLMUnavailable("decision model off")
        if self._down_until.get("decide", 0) > time.time():
            raise LLMUnavailable("decision model down")
        body = {"model": self.cfg.decision_model, "state": state, "questions": questions, "keep_alive": "30m"}
        try:
            r = self.client.post(f"{self.cfg.ollama_url.rstrip('/')}/v1/systemone", json=body)
            r.raise_for_status()
            payload = r.json()
            return payload["answers"], payload.get("usage", {}).get("input_tokens", 0)
        except httpx.ConnectError as e:
            self._down_until["decide"] = time.time() + 30
            raise LLMUnavailable(f"decision model: {e}") from e
        except httpx.HTTPStatusError as e:
            # 404 / 400: the model isn't pulled or isn't a decision model; don't retry on every write.
            self._down_until["decide"] = time.time() + (600 if e.response.status_code in (400, 404) else 30)
            raise LLMUnavailable(f"decision model: {e}") from e
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise LLMUnavailable(f"decision model: {e}") from e

    def _call(self, hop: ProviderRef, system: str, user: str, schema: type[BaseModel], num_predict: int = 2048) -> LLMResult:
        started = time.perf_counter()
        json_schema = schema.model_json_schema()
        if hop.provider == "ollama":
            body: dict[str, Any] = {
                "model": hop.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "format": json_schema,
                "stream": False,
                # Thinking models (qwen3.5, gpt-oss) otherwise reason for minutes before emitting JSON.
                "think": False,
                "keep_alive": "30m",  # a model reloaded between calls costs seconds
                "options": {"temperature": 0, "num_predict": num_predict},
            }
            r = self.client.post(f"{(hop.base_url or self.cfg.ollama_url).rstrip('/')}/api/chat", json=body)
            r.raise_for_status()
            payload = r.json()
            content = payload["message"]["content"]
            tin, tout = payload.get("prompt_eval_count", 0), payload.get("eval_count", 0)
        elif hop.provider == "openai_compat":
            key = os.environ.get(hop.api_key_env or "", "")
            body = {
                "model": hop.model,
                "messages": [
                    {"role": "system", "content": f"{system}\n\nRespond with JSON matching this schema:\n{json.dumps(json_schema)}"},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0,
                "max_tokens": num_predict,
            }
            r = self.client.post(
                f"{hop.base_url.rstrip('/')}/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"}
            )
            r.raise_for_status()
            payload = r.json()
            content = payload["choices"][0]["message"]["content"]
            usage = payload.get("usage", {})
            tin, tout = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        else:
            raise KeyError(f"unknown provider {hop.provider}")
        return LLMResult(
            data=json.loads(content),
            model=hop.model,
            tokens_in=tin,
            tokens_out=tout,
            latency_ms=(time.perf_counter() - started) * 1000,
        )


class FakeLLM(LLM):
    """Deterministic stand-in for tests: `handler(role, user_prompt) -> dict`."""

    def __init__(self, cfg: Config, handler: Callable[[str, str], dict[str, Any]]):
        self.cfg = cfg
        self.handler = handler

    def decide(self, state: str, questions: dict[str, dict]) -> tuple[dict[str, dict], int]:
        raise LLMUnavailable("fake: no decision model")

    def complete(self, role: str, system: str, user: str, schema: type[T], *, accept: Callable[[T], bool] | None = None,
                 num_predict: int = 2048) -> tuple[T, LLMResult]:  # fmt: skip
        data = self.handler(role, user)
        if data is None:
            raise LLMUnavailable("fake: no answer")
        return schema.model_validate(data), LLMResult(data, "fake", len(user) // 4, 20, 0.0)
