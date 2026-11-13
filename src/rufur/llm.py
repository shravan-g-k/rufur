"""Role-routed structured LLM calls: local Ollama first, free cloud as fallback.

Every call asks for JSON matching a pydantic model. Output that fails to
validate is retried once on the same hop, then the next hop in the role's
chain is tried. Local Ollama models (`cfg.local_fallback`) always end the chain, so a cloud
rate limit or outage falls back to them. If the whole chain fails the caller
gets `LLMUnavailable` and must fall back to a safe deterministic outcome
(reject or "unclear"). Fallbacks and failures go to `on_log` (the `llm_log`
table, shown in the UI).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .config import Config, ProviderRef, api_keys

T = TypeVar("T", bound=BaseModel)

# A 429 for a daily quota (Groq: "tokens per day (TPD)", "requests per day (RPD)") means this key is spent for
# hours, so the next key takes over. A per-minute limit keeps the key; the provider just cools down.
_DAILY_LIMIT = re.compile(r"per day|\((?:TPD|RPD|ASD)\)", re.I)
_TRY_AGAIN = re.compile(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", re.I)


def _retry_after(r: httpx.Response, default: float) -> float:
    if h := r.headers.get("retry-after"):
        try:
            return float(h)
        except ValueError:
            pass
    if (m := _TRY_AGAIN.search(r.text)) and any(m.groups()):
        hrs, mins, secs = (float(g or 0) for g in m.groups())
        return hrs * 3600 + mins * 60 + secs
    return default


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
        self._key_spent_until: dict[str, float] = {}  # API keys that hit their daily limit
        # Called with one record per call that needed a fallback or failed outright.
        self.on_log: Callable[[dict], None] | None = None

    def chain(self, role: str) -> list[ProviderRef]:
        hops = list(self.cfg.roles.get(role, []))
        have = {(h.provider, h.model) for h in hops}
        return hops + [ProviderRef("ollama", m) for m in self.cfg.local_fallback if ("ollama", m) not in have]

    def _pick_key(self, hop: ProviderRef) -> tuple[str | None, str]:
        """The first configured key that hasn't hit its daily limit, or None and why."""
        keys = api_keys(hop.api_key_env, self.cfg.home)
        if not keys:
            return None, f"{hop.api_key_env} not set (put it, or {hop.api_key_env}S=key1,key2,..., in ~/.rufur/.env)"
        t = time.time()
        if live := [k for k in keys if self._key_spent_until.get(k, 0) <= t]:
            return live[0], ""
        back = min(self._key_spent_until[k] for k in keys) - t
        return None, f"all {len(keys)} {hop.api_key_env} keys hit their daily limit (first back in {back / 60:.0f} min)"

    def _log(self, role: str, outcome: str, model: str | None, errors: list[str]) -> None:
        if self.on_log and errors:
            try:
                self.on_log({"role": role, "outcome": outcome, "model": model, "errors": errors})
            except Exception:  # logging must never break the call it describes
                pass

    def complete(self, role: str, system: str, user: str, schema: type[T], *, accept: Callable[[T], bool] | None = None,
                 num_predict: int = 2048) -> tuple[T, LLMResult]:  # fmt: skip
        """`accept` rejects an answer that parses but is unusable; the next model in the chain gets a go.
        If no model passes it, the first parsed answer is returned and the caller deals with the bad parts."""
        if self.cfg.offline:
            raise LLMUnavailable("offline mode")
        errors: list[str] = []
        fallback: tuple[T, LLMResult] | None = None
        for hop in self.chain(role):
            hop_key = f"{hop.provider}:{hop.base_url or self.cfg.ollama_url}"
            if (wait := self._down_until.get(hop_key, 0) - time.time()) > 0:
                errors.append(f"{hop.model}: skipped, rate-limited or unreachable (retry in {wait:.0f}s)")
                continue
            attempts = 0
            while attempts < 3:
                key = None
                if hop.api_key_env:
                    key, why = self._pick_key(hop)
                    if key is None:
                        errors.append(f"{hop.model}: {why}")
                        break
                try:
                    result = self._call(hop, system, user, schema, num_predict, key=key)
                    parsed = schema.model_validate(result.data)
                    if accept is None or accept(parsed):
                        self._log(role, "fallback", hop.model, errors)
                        return parsed, result
                    fallback = fallback or (parsed, result)
                    errors.append(f"{hop.model}: answer rejected")
                    break
                except (ValidationError, json.JSONDecodeError) as e:
                    errors.append(f"{hop.model}: invalid output ({e.__class__.__name__})")
                    attempts += 1
                except httpx.ConnectError as e:
                    self._down_until[hop_key] = time.time() + 30
                    errors.append(f"{hop.model}: {e}")
                    break
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 429 and key and _DAILY_LIMIT.search(e.response.text):
                        # This key's daily quota is spent: park it until it resets and retry with the next key.
                        self._key_spent_until[key] = time.time() + _retry_after(e.response, 3600)
                        errors.append(f"{hop.model}: key ...{key[-4:]} hit its daily limit, switching key")
                        continue
                    errors.append(f"{hop.model}: {e.response.status_code} {e.response.text[:200]}")
                    if e.response.status_code == 429:
                        # Rate limited: skip this provider until it says to come back, and let the next hop answer now.
                        self._down_until[hop_key] = time.time() + min(_retry_after(e.response, 30), 300)
                    break
                except (httpx.HTTPError, KeyError) as e:
                    errors.append(f"{hop.model}: {e}")
                    break
        if fallback:
            self._log(role, "fallback", fallback[1].model, errors)
            return fallback
        self._log(role, "failed", None, errors or [f"no usable provider for role {role!r}"])
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

    def _call(self, hop: ProviderRef, system: str, user: str, schema: type[BaseModel], num_predict: int = 2048,
              key: str | None = None) -> LLMResult:  # fmt: skip
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
            if "gpt-oss" in hop.model:
                # A reasoning model: its hidden reasoning counts against max_tokens, and at the default effort a
                # long chunk used the whole budget before writing any JSON (Groq: json_validate_failed).
                body["reasoning_effort"] = "low"
                body["max_tokens"] = num_predict + 2048
            r = self.client.post(
                f"{hop.base_url.rstrip('/')}/chat/completions", json=body, headers={"Authorization": f"Bearer {key or ''}"}
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
