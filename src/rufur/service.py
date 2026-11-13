"""MemoryService: the only place with business logic.

REST and MCP are thin adapters over this class, so both APIs always expose
exactly the same behaviour. Every public method takes the calling agent's name
first; access control is applied here, before any search or write.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .config import Config, api_keys
from .db.conn import Database, dumps
from .embed import EmbedUnavailable, make_embedder
from .events_bus import EventBus
from .forget import engine as forget_engine
from .forget import fade as fade_mod
from .forget import receipts as receipts_mod
from .forget.purge import purge_memories
from . import handover as ho
from .ledger import Ledger
from .llm import LLM, LLMUnavailable
from .read.recall import CONTEXT_HEADER, fast_recall, render
from .read.rerank import RerankUnavailable, make_reranker
from .read.search import Filter
from .read.verify import make_verifier
from .read import working_set as ws_mod
from . import dashboard as dash_mod, pools as pools_mod, raw as raw_mod, stm as stm_mod
from .write import consolidate as consolidate_mod
from .write import resolve as resolve_mod
from .write import review as review_mod
from .textutil import estimate_tokens, new_id, now, quote_in, sha256
from .write.chunk import at_least, detect_sensitivity, skip_reason
from .write.extract import extract_claims
from .write.importance import retention_score
from .write.pipeline import ClaimIn, WritePath
from .write.trust import ROLE_SOURCE, SENSITIVITY_ORDER, TRUST_ORDER, mask_secrets


log = logging.getLogger("rufur.service")


class AccessDenied(PermissionError):
    pass


class NotFound(LookupError):
    pass


def _acl_allows(acl: dict, perm: str, agent: str, user_id: str | None = None) -> bool:
    """ACL entries are "*", an agent name, or "user:<id>" (every agent of that user).

    Subagent identities (user "scratch:<pool>") never match "*": they may see only what their pool grants.
    """
    allowed = acl.get(perm, [])
    scratch = user_id is not None and user_id.startswith("scratch:")
    return (("*" in allowed and not scratch) or agent in allowed
            or (user_id is not None and f"user:{user_id}" in allowed))  # fmt: skip


class MemoryService:
    def __init__(self, cfg: Config, llm: LLM | None = None, embedder=None):
        cfg.ensure_dirs()
        self.cfg = cfg
        self.bus = EventBus()
        self.embedder = embedder or make_embedder(cfg)
        self.db = Database(cfg.db_path, cfg.embed_dim)
        self.ledger = Ledger(self.db, self.bus)
        self.llm = llm or LLM(cfg)
        self.llm.on_log = self._log_llm
        self.writer = WritePath(cfg, self.db, self.ledger, self.llm, self.embedder)
        self.verifier = make_verifier(self.llm)
        self.fast_reranker = make_reranker(cfg, cfg.fast_reranker)
        self.deep_reranker = make_reranker(cfg, cfg.deep_reranker)
        self.working_sets = ws_mod.WorkingSets()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rufur-extract")
        self._idle_queued: set[str] = set()
        self._idle_lock = threading.Lock()
        self._check_embedder()
        self.ensure_pool(cfg.default_pool, owner=None, description="Default long-term pool")
        self.stm_pool(cfg.default_user)

    def _check_embedder(self) -> None:
        stored = self.db.meta("embedder")
        if stored is None:
            self.db.set_meta("embedder", self.embedder.name)
        elif stored != self.embedder.name:
            raise RuntimeError(f"database vectors come from {stored!r} but the configured embedder is {self.embedder.name!r}")

    def _log_llm(self, rec: dict) -> None:
        self.db.execute("INSERT INTO llm_log(role, outcome, model, errors, created_at) VALUES(?,?,?,?,?)",
                        (rec["role"], rec["outcome"], rec["model"], json.dumps(rec["errors"]), now()))  # fmt: skip
        if rec["outcome"] == "failed":
            log.warning("LLM %s failed: %s", rec["role"], "; ".join(rec["errors"]))
        self.bus.publish("llm.log", role=rec["role"], outcome=rec["outcome"], model=rec["model"])

    def llm_log(self, limit: int = 200) -> dict:
        rows = self.db.all("SELECT * FROM llm_log ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["errors"] = json.loads(r["errors"])
        day = now() - 86400
        counts = {o: self.db.scalar("SELECT count(*) FROM llm_log WHERE outcome = ? AND created_at > ?", (o, day))
                  for o in ("fallback", "failed")}  # fmt: skip
        if not isinstance(self.llm, LLM):
            return {"rows": rows, "last_24h": counts, "chains": {}, "keys": {}}
        keys = {}
        for env in {h.api_key_env for hops in self.cfg.roles.values() for h in hops if h.api_key_env}:
            have = api_keys(env, self.cfg.home)
            spent = [self.llm._key_spent_until[k] for k in have if self.llm._key_spent_until.get(k, 0) > now()]
            keys[env] = {"total": len(have), "spent": len(spent), "next_back": min(spent) if spent else None}
        return {"rows": rows, "last_24h": counts, "keys": keys,
                "chains": {role: [h.model for h in self.llm.chain(role)] for role in self.cfg.roles}}  # fmt: skip

    def close(self) -> None:
        self.worker.shutdown(wait=True)
        self.db.close()

    # ---------------------------------------------------------------- agents

    def agent(self, name: str) -> dict:
        a = self.db.one("SELECT * FROM agents WHERE name = ?", (name,))
        if a is None:
            self.db.execute(
                "INSERT OR IGNORE INTO agents(id, name, user_id, created_at) VALUES(?,?,?,?)",
                (new_id("agent"), name, self.cfg.default_user, now()),
            )
            a = self.db.one("SELECT * FROM agents WHERE name = ?", (name,))
        self.db.execute("UPDATE agents SET last_seen = ? WHERE name = ?", (now(), name))
        return a

    def create_agent(self, name: str, *, trust: str = "medium_low", clearance: str = "confidential") -> dict:
        if trust not in TRUST_ORDER or clearance not in SENSITIVITY_ORDER:
            raise ValueError("invalid trust or clearance")
        key = "rk_" + secrets.token_urlsafe(24)
        self.agent(name)
        self.db.execute(
            "UPDATE agents SET key_hash = ?, trust = ?, clearance = ? WHERE name = ?", (sha256(key), trust, clearance, name)
        )
        self.ledger.audit(name, "agent.create", name, {"trust": trust, "clearance": clearance})
        return {"name": name, "api_key": key, "trust": trust, "clearance": clearance}

    def agent_for_key(self, key: str) -> str | None:
        return self.db.scalar("SELECT name FROM agents WHERE key_hash = ?", (sha256(key),))

    def list_agents(self) -> list[dict]:
        return self.db.all("SELECT name, user_id, trust, clearance, quarantined, created_at, last_seen FROM agents ORDER BY created_at")

    def quarantine_agent(self, actor: str, name: str) -> dict:
        """Mark an agent compromised and bulk-quarantine everything it authored (plan §8)."""
        self.db.execute("UPDATE agents SET quarantined = 1 WHERE name = ?", (name,))
        rows = self.db.all("SELECT id FROM memories WHERE author_agent = ? AND status IN ('active','disputed')", (name,))
        for r in rows:
            self.ledger.update_memory(r["id"], status="quarantined",
                                      status_reason=f"the agent that wrote it ({name}) was quarantined by {actor}")  # fmt: skip
            self.ledger.op("quarantine", r["id"], actor, {"reason": "agent_quarantined"})
        self.ledger.audit(actor, "agent.quarantine", name, {"memories": len(rows)})
        return {"agent": name, "quarantined_memories": len(rows)}

    # ----------------------------------------------------------------- pools

    def ensure_pool(self, name: str, *, owner: str | None, kind: str = "long", description: str | None = None,
                    acl: dict | None = None) -> dict:  # fmt: skip
        p = self.db.one("SELECT * FROM pools WHERE name = ?", (name,))
        if p is None:
            pid = new_id("pool")
            self.db.execute(
                "INSERT INTO pools(id, name, kind, owner, description, retention_days, acl, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (pid, name, kind, owner, description, self.cfg.retention_days,
                 dumps(acl or {"read": ["*"], "write": ["*"]}), now()),  # fmt: skip
            )
            p = self.db.one("SELECT * FROM pools WHERE id = ?", (pid,))
        return p

    def create_pool(self, agent: str, name: str, *, description: str | None = None, acl: dict | None = None) -> dict:
        if self.db.scalar("SELECT 1 FROM pools WHERE name = ?", (name,)):
            raise ValueError(f"pool {name!r} already exists")
        acl = acl or {"read": [agent], "write": [agent]}
        p = self.ensure_pool(name, owner=agent, description=description, acl=acl)
        self.ledger.audit(agent, "pool.create", p["id"], {"acl": acl})
        return self._pool_out(p)

    def set_pool_acl(self, agent: str, name: str, acl: dict) -> dict:
        p = self._pool(name)
        if p["owner"] not in (None, agent):
            raise AccessDenied("only the pool owner can change its ACL")
        self.db.execute("UPDATE pools SET acl = ? WHERE id = ?", (dumps(acl), p["id"]))
        self.ledger.audit(agent, "pool.acl", p["id"], {"acl": acl})
        return self._pool_out(self._pool(name))

    def _pool(self, name_or_id: str) -> dict:
        p = self.db.one("SELECT * FROM pools WHERE name = ? OR id = ?", (name_or_id, name_or_id))
        if p is None:
            raise NotFound(f"pool {name_or_id!r} not found")
        return p

    def _pool_out(self, p: dict) -> dict:
        return {**p, "acl": json.loads(p["acl"]), "memories": self.db.scalar("SELECT count(*) FROM memories WHERE pool_id = ?", (p["id"],))}

    def list_pools(self, agent: str) -> list[dict]:
        a = self.agent(agent)
        pools = self.db.all("SELECT * FROM pools WHERE kind IN ('long', 'stm') AND closed_at IS NULL ORDER BY created_at")
        return [self._pool_out(p) for p in pools if _acl_allows(json.loads(p["acl"]), "read", agent, a["user_id"])]

    def stm_pool(self, user_id: str) -> dict:
        """The user's short-term memory, shared by every agent of that user."""
        principal = f"user:{user_id}"
        return self.ensure_pool(f"stm:{user_id}", owner=principal, kind="stm", description="Short-term memory",
                                acl={"read": [principal], "write": [principal]})  # fmt: skip

    def _readable(self, agent: str) -> tuple[list[str], list[str], list[str]]:
        """(long-term pool ids, STM pool ids, scratch pool ids) this agent may read."""
        a = self.agent(agent)
        long_ids, stm_ids, scratch_ids = [], [], []
        for p in self.db.all("SELECT id, kind, acl FROM pools WHERE closed_at IS NULL"):
            if _acl_allows(json.loads(p["acl"]), "read", agent, a["user_id"]):
                {"stm": stm_ids, "scratch": scratch_ids}.get(p["kind"], long_ids).append(p["id"])
        return long_ids, stm_ids, scratch_ids

    def readable_pools(self, agent: str) -> list[str]:
        long_ids, stm_ids, scratch_ids = self._readable(agent)
        return [*long_ids, *stm_ids, *scratch_ids]

    def _filter(self, agent: str, *, pool: str | None = None, **kw) -> Filter:
        """Everything this agent may see, as one search filter (applied before ranking)."""
        a = self.agent(agent)
        long_ids, stm_ids, scratch_ids = self._readable(agent)
        direct = long_ids + scratch_ids
        if pool:
            only = self._pool(pool)["id"]
            direct = [pid for pid in direct if pid == only]
        kw.setdefault("clearance", a["clearance"])
        kw.setdefault("min_trust", self.cfg.min_trust)
        return Filter(pool_ids=direct, stm_pool_ids=stm_ids, member_pool_ids=scratch_ids,
                      default_target=self._pool(self.cfg.default_pool)["id"], **kw)  # fmt: skip

    def _home_pool(self, agent: str) -> dict:
        """Where an agent's new memories land: its scratch pool if it's a subagent, else its user's STM."""
        a = self.agent(agent)
        if a["user_id"].startswith("scratch:"):
            return self._pool(a["user_id"].split(":", 1)[1])
        return self.stm_pool(a["user_id"])

    def _writable(self, agent: str, pool: str | None) -> dict:
        p = self._pool(pool or self.cfg.default_pool)
        if not _acl_allows(json.loads(p["acl"]), "write", agent, self.agent(agent)["user_id"]):
            raise AccessDenied(f"agent {agent!r} cannot write to pool {p['name']!r}")
        return p

    def _can_read_memory(self, agent: str, mem: dict) -> bool:
        flt = self._filter(agent, statuses=(mem["status"],), min_trust="low", current_only=False)
        where, params = flt.sql()
        return bool(self.db.scalar(f"SELECT 1 FROM memories m WHERE m.id = ? AND {where}", (mem["id"], *params)))

    # -------------------------------------------------------------- sessions

    def session_start(self, agent: str, pool: str | None = None, task_id: str | None = None) -> dict:
        target = self._writable(agent, pool) if pool else self._pool(self.cfg.default_pool)
        sid = new_id("sess")
        # Events and extracted memories go to the agent's home: the user's shared STM (or a scratch pool).
        home = self._home_pool(agent)
        self.db.execute(
            "INSERT INTO sessions(id, agent, pool_id, target_pool, task_id, started_at) VALUES(?,?,?,?,?,?)",
            (sid, agent, home["id"], target["id"], task_id, now()),
        )
        self.ledger.audit(agent, "session.start", sid)
        self.bus.publish("session.started", id=sid, agent=agent)
        return {"session_id": sid, "pool": target["name"]}

    def _session(self, session_id: str) -> dict:
        s = self.db.one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if s is None:
            raise NotFound(f"session {session_id!r} not found")
        return s

    def log_event(self, agent: str, content: str, *, role: str = "user", session_id: str | None = None,
                  pool: str | None = None, source_type: str | None = None, background: bool = True) -> dict:  # fmt: skip
        if session_id:
            s = self._session(session_id)
            if s["ended_at"]:
                raise ValueError("session already ended")
            pool_id = s["target_pool"]
        else:
            pool_id = self._writable(agent, pool)["id"]
        ev = self.ledger.add_event(pool_id=pool_id, agent=agent, role=role,
                                   source_type=source_type or ROLE_SOURCE.get(role, "tool"),
                                   content=content, session_id=session_id)  # fmt: skip
        checkpoint = False
        if reason := self._skip_reason(ev, session_id):
            # Chit-chat, requests and repeats never reach a model; they stay in the transcript only.
            self.db.execute("UPDATE events SET extracted = 1, skip_reason = ? WHERE id = ?", (reason, ev["id"]))
            return {"event_id": ev["id"], "checkpoint": False, "skipped": True, "skip_reason": reason}
        if session_id:
            pending = self.db.scalar("SELECT count(*) FROM events WHERE session_id = ? AND extracted = 0", (session_id,))
            if pending >= self.cfg.checkpoint_every:
                checkpoint = True
                self._run(background, self.extract_session, session_id)
        return {"event_id": ev["id"], "checkpoint": checkpoint}

    def _skip_reason(self, ev: dict, session_id: str | None) -> str | None:
        """Why this message isn't worth a model call, or None if it is."""
        if reason := skip_reason(ev["content"], ev["role"], self.cfg.chunk_chars):
            return reason
        if session_id and self.db.scalar(
            "SELECT 1 FROM events WHERE session_id = ? AND content_hash = ? AND id != ? LIMIT 1",
            (session_id, ev["content_hash"], ev["id"]),
        ):
            return "repeat of an earlier message in this session"
        return None

    def warm(self) -> None:
        """Load the embedder and fast reranker now, so the first hook of a session isn't the one that pays for it."""
        def load() -> None:
            for fn in (lambda: self.embedder.embed(["warm up"]),
                       lambda: self.fast_reranker is not None and self.fast_reranker.score("warm up", ["warm up"])):  # fmt: skip
                try:
                    fn()
                except Exception:  # warming is best effort; the first real call retries
                    pass

        threading.Thread(target=load, name="rufur-warm", daemon=True).start()

    def extract_idle_sessions(self) -> int:
        """Sessions that went quiet get their pending events extracted without waiting for a full checkpoint."""
        rows = self.db.all(
            "SELECT e.session_id FROM events e JOIN sessions s ON s.id = e.session_id "
            "WHERE e.extracted = 0 AND s.ended_at IS NULL GROUP BY e.session_id HAVING max(e.created_at) < ?",
            (now() - self.cfg.extract_idle_s,),
        )
        queued = 0
        for r in rows:
            sid = r["session_id"]
            with self._idle_lock:
                if sid in self._idle_queued:  # still waiting for (or running) the last tick's job
                    continue
                self._idle_queued.add(sid)
            self.worker.submit(self._extract_idle, sid)
            queued += 1
        return queued

    def _extract_idle(self, session_id: str) -> None:
        try:
            self.extract_session(session_id)
        except Exception:  # a broken session must not kill the worker; the next quiet period retries
            log.exception("idle extraction failed for %s", session_id)
        finally:
            with self._idle_lock:
                self._idle_queued.discard(session_id)

    def _run(self, background: bool, fn, *args):
        if background:
            return self.worker.submit(fn, *args)
        return fn(*args)

    def extract_session(self, session_id: str) -> dict:
        """Checkpoint: extract claims from unextracted events straight into short-term memory."""
        s = self._session(session_id)
        events = self.db.all(
            "SELECT * FROM events WHERE session_id = ? AND extracted = 0 ORDER BY created_at", (session_id,)
        )
        if not events:
            return {"stored": 0, "rejected": 0}
        started = time.perf_counter()
        try:
            accepted, rejected, usage = extract_claims(self.llm, events)
        except LLMUnavailable as e:
            # Leave events unextracted; the next checkpoint or session end retries.
            return {"stored": 0, "rejected": 0, "queued": len(events), "error": str(e)}
        agent = self.agent(s["agent"])
        results = []
        for claim, ev in accepted:
            res = self.writer.commit(_claim_in(claim), pool_id=s["pool_id"], agent=agent, source_type=ev["source_type"],
                                     sources=[(ev["id"], claim.quote)], session_id=session_id, tier="stm",
                                     target_pool=s["target_pool"], candidate_pools=[s["pool_id"], s["target_pool"]])  # fmt: skip
            results.append(self._settle(res.summary()))
        marks = ",".join("?" * len(events))
        self.db.execute(f"UPDATE events SET extracted = 1 WHERE id IN ({marks})", tuple(e["id"] for e in events))
        # Say why a message that reached the model left nothing in short-term memory.
        outcomes: dict[str, list[str]] = {}
        for (_claim, ev), r in zip(accepted, results):
            outcomes.setdefault(ev["id"], []).append(r["outcome"])
        for ev in events:
            got = outcomes.get(ev["id"], [])
            if "created" in got or "corroborated" in got:
                continue
            if not got:
                reason = "the model found nothing durable in it"
            elif "tombstoned" in got:
                reason = "it repeats something you asked to forget"
            else:
                reason = "already in memory (counted as a duplicate)"
            self.db.execute("UPDATE events SET skip_reason = ? WHERE id = ?", (reason, ev["id"]))
        self.ledger.metric("extract", (time.perf_counter() - started) * 1000, path=usage.model if usage else "gated",
                           agent=s["agent"], tokens_in=sum(estimate_tokens(e["content"]) for e in events),
                           llm_tokens=usage.tokens_in + usage.tokens_out if usage else 0,
                           meta={"accepted": len(accepted), "rejected": len(rejected)})  # fmt: skip
        self._guard(s["agent"])
        self._maybe_size_trigger(agent["user_id"])
        return {"stored": sum(r["outcome"] == "created" for r in results), "rejected": len(rejected),
                "model": usage.model if usage else None, "results": results}  # fmt: skip

    def session_end(self, agent: str, session_id: str, background: bool = False) -> dict:
        s = self._session(session_id)
        if s["agent"] != agent:
            raise AccessDenied("only the session's agent can end it")
        if s["ended_at"]:
            return {"session_id": session_id, "already_ended": True}
        self.db.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (now(), session_id))
        fut = self._run(background, self._finish_session, session_id)
        return {"session_id": session_id, "scheduled": True} if background else fut

    def _finish_session(self, session_id: str) -> dict:
        """Extract the tail of the session into STM. Filing into long-term stores is consolidation's job."""
        extraction = self.extract_session(session_id)
        self.ledger.audit(self._session(session_id)["agent"], "session.end", session_id,
                          {"stored": extraction.get("stored", 0)})  # fmt: skip
        self.bus.publish("session.ended", id=session_id, stored=extraction.get("stored", 0))
        dropped = self.drop_if_empty(session_id)
        return {"session_id": session_id, "extraction": extraction, "dropped_empty": dropped}

    def drop_if_empty(self, session_id: str, *, abandoned: bool = False) -> bool:
        """Delete a session that never produced a memory. Every Claude Code start, /clear or one-off `claude -p`
        opens one (the SessionStart hook needs it to preload memory), so most short ones end up empty.
        Its messages stay as unsessioned events: the daily journal and recall_past still read them.
        `abandoned` lets the sweeper drop a session that was never ended."""
        s = self.db.one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if s is None or not (s["ended_at"] or abandoned):
            return False
        keep = self.db.scalar(
            "SELECT 1 WHERE EXISTS (SELECT 1 FROM memories WHERE session_id = :s)"
            " OR EXISTS (SELECT 1 FROM ops WHERE op = 'create' AND json_extract(payload, '$.session') = :s)"
            " OR EXISTS (SELECT 1 FROM pools WHERE owner_session = :s)"
            " OR EXISTS (SELECT 1 FROM events WHERE session_id = :s AND extracted = 0)",  # extraction still pending
            {"s": session_id},
        )
        if keep:
            return False
        self.db.execute("UPDATE events SET session_id = NULL WHERE session_id = ?", (session_id,))
        self.db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        self.ledger.audit(s["agent"], "session.dropped_empty", session_id)
        return True

    # ---------------------------------------------------------------- writes

    def remember(self, agent: str, text: str, *, type: str | None = None, subject: str = "", predicate: str = "",
                 value: str = "", source: str = "agent", pool: str | None = None, sensitivity: str = "internal",
                 pinned: bool = False, session_id: str | None = None, long_term: bool = False, wait: bool = True) -> dict:  # fmt: skip
        """Explicit write into short-term memory (or a subagent's scratch pool).

        With `wait=False`, text that needs structuring is logged now and written by the background worker.

        Without a subject/predicate key, the extract model structures the text first. `pool` names the
        long-term pool the memory will be filed into; `long_term=True` files it there immediately.
        """
        started = time.perf_counter()
        a = self.agent(agent)
        home = self._home_pool(agent)
        scratch = home["kind"] == "scratch"
        if scratch and pool:
            raise AccessDenied("a subagent can only write to its own scratch pool")
        target = None if scratch else self._writable(agent, pool)["id"]
        p = home
        source_type = source
        flags_extra: list[str] = []
        if source == "user" and agent != "ui":
            # An agent relaying the user is believed only if a logged user turn contains the text.
            seen = self.db.all(
                "SELECT content FROM events WHERE agent = ? AND role = 'user' AND created_at > ? ORDER BY created_at DESC LIMIT 50",
                (agent, now() - 86400),
            )
            if not any(quote_in(text, e["content"]) or quote_in(value, e["content"]) for e in seen if value or text):
                source_type = "document"  # medium trust: plausible but unverified
                flags_extra.append("unverified_user_claim")
        ev = self.ledger.add_event(pool_id=p["id"], agent=agent, role="user" if source_type == "user" else "assistant",
                                   source_type=source_type, content=text, session_id=session_id)  # fmt: skip
        self.db.execute("UPDATE events SET extracted = 1 WHERE id = ?", (ev["id"],))

        def write() -> dict:
            return self._remember_write(a, ev, text, p=p, target=target, scratch=scratch, type=type, subject=subject,
                                        predicate=predicate, value=value, source_type=source_type, flags_extra=flags_extra,
                                        sensitivity=sensitivity, pinned=pinned, session_id=session_id, long_term=long_term,
                                        started=started)  # fmt: skip

        if not wait and not (subject and predicate):
            # Structuring free text takes a model call; the agent shouldn't wait for it.
            self.worker.submit(self._quietly, write)
            return {"queued": True, "event_id": ev["id"]}
        return write()

    def _quietly(self, fn) -> None:
        try:
            fn()
        except Exception:  # a failed background write must not kill the worker; the event stays in the transcript
            log.exception("background write failed")

    def _remember_write(self, a: dict, ev: dict, text: str, *, p: dict, target: str | None, scratch: bool, type: str | None,
                        subject: str, predicate: str, value: str, source_type: str, flags_extra: list[str],
                        sensitivity: str, pinned: bool, session_id: str | None, long_term: bool, started: float) -> dict:  # fmt: skip
        agent = a["name"]
        claims: list[ClaimIn] = []
        llm_tokens = 0
        if not (subject and predicate):
            try:
                # The agent asked for this to be kept, so no salience gate; chunking still applies.
                accepted, _rej, usage = extract_claims(self.llm, [ev], gate=False)
                llm_tokens = usage.tokens_in + usage.tokens_out if usage else 0
                claims = [_claim_in(c) for c, _ in accepted]
            except LLMUnavailable:
                pass
        if not claims:
            claims = [ClaimIn(type=type or "fact", text=text, subject=subject, predicate=predicate, value=value or text,
                              quote=text, sensitivity=at_least(sensitivity, detect_sensitivity(text)))]  # fmt: skip
        results = []
        for c in claims:
            if type:
                c.type = type
            c.pinned = pinned
            if SENSITIVITY_ORDER.index(sensitivity) > SENSITIVITY_ORDER.index(c.sensitivity):
                c.sensitivity = sensitivity  # caller may raise sensitivity, never lower it
            res = self.writer.commit(c, pool_id=p["id"], agent=a, source_type=source_type,
                                     sources=[(ev["id"], c.quote or text)], session_id=session_id, tier="stm",
                                     target_pool=target, candidate_pools=[p["id"]] + ([target] if target else []))  # fmt: skip
            if res.outcome == "created" and flags_extra:
                current = self.ledger.get_memory(res.memory["id"])["flags"]
                self.ledger.update_memory(res.memory["id"], flags=sorted(set(current) | set(flags_extra)))
            llm_tokens += res.llm_tokens
            results.append(self._settle(res.summary()))
        self.ledger.metric("remember", (time.perf_counter() - started) * 1000, path="write", agent=agent,
                           tokens_in=estimate_tokens(text), llm_tokens=llm_tokens)  # fmt: skip
        self._guard(agent)
        out: dict = {"results": results, "event_id": ev["id"]}
        if long_term and not scratch:
            ids = [r["memory"]["id"] for r in results if r["outcome"] == "created" and r["memory"]["status"] != "quarantined"]
            if ids:
                out["consolidation"] = consolidate_mod.consolidate(self, a["user_id"], "agent", memory_ids=ids)
        return out

    def confirm_memory(self, agent: str, memory_id: str, action: str = "confirm") -> dict:
        """Accept or reject one memory. Conflicts it's part of are resolved in its favour (or against it).

        Raising a memory to user-confirmed needs the user in the loop: the UI, or an agent with a fresh user
        message. Otherwise an agent's confirmation counts as the agent's own decision and can't raise trust.
        """
        m = self._readable_memory(agent, memory_id)
        if action not in ("confirm", "reject"):
            raise ValueError("action must be 'confirm' or 'reject'")
        by = "user" if self._user_present(agent) else "agent"
        open_ids = [r["id"] for r in self.db.all("SELECT id, memory_ids FROM conflicts WHERE status = 'open'")
                    if memory_id in json.loads(r["memory_ids"])]  # fmt: skip
        if open_ids:
            out = []
            for cid in open_ids:
                if action == "confirm":
                    out.append(self.resolve_conflict(agent, cid, action="keep", winner=memory_id, decided_by=by))
                else:
                    others = [x for x in json.loads(self.db.scalar("SELECT memory_ids FROM conflicts WHERE id = ?", (cid,)))
                              if x != memory_id]  # fmt: skip
                    out.append(self.resolve_conflict(agent, cid, action="keep", winner=others[0], decided_by=by)
                               if others else {})  # fmt: skip
            return {"id": memory_id, "resolved_conflicts": out}
        if action == "reject":
            self.ledger.update_memory(memory_id, status="rejected", valid_to=now())
            self.ledger.op("reject", memory_id, agent)
            return {"id": memory_id, "status": "rejected"}
        if m["status"] == "quarantined" and by != "user":
            raise AccessDenied("held memories can only be approved by the user; ask them and pass their answer")
        days = consolidate_mod.REVALIDATE_DAYS.get(m["volatility"])
        updates: dict = {"status": "active", "revalidate_at": now() + days * 86400 if days else None}
        if by == "user":
            updates.update(trust="high", flags=sorted(set(m["flags"]) | {"user_confirmed"}))
        self.ledger.update_memory(memory_id, **updates)
        self.ledger.op("confirm", memory_id, agent, {"by": by})
        self.ledger.audit(agent, "memory.confirm", memory_id, {"by": by})
        return {"id": memory_id, "status": "active", "decided_by": by}

    # ------------------------------------------------------------- conflicts

    def _settle(self, summary: dict) -> dict:
        """Give the memory layer its own go at a new conflict or held memory; report what happened to the writer."""
        if summary.get("conflict_id"):
            summary["conflict"] = resolve_mod.auto_resolve(self, summary["conflict_id"])
        elif summary.get("memory") and summary["memory"]["status"] == "quarantined":
            summary["review"] = review_mod.review(self, summary["memory"]["id"])
        if summary.get("memory"):
            summary["memory"]["status"] = self.ledger.get_memory(summary["memory"]["id"])["status"]
        return summary

    def auto_review(self, agent: str) -> dict:
        """Let the memory layer take another pass at everything waiting on review: open conflicts it hasn't
        already handed to the user, and held memories."""
        a = self.agent(agent)
        conflicts = []
        for r in self.db.all("SELECT id FROM conflicts WHERE user_id = ? AND status = 'open' AND ask_user = 0",
                             (a["user_id"],)):  # fmt: skip
            try:
                self._conflict_visible(agent, r["id"])
            except NotFound:
                continue
            res = resolve_mod.auto_resolve(self, r["id"])
            conflicts.append({"conflict_id": r["id"], "status": res["status"], "ask_user": res.get("ask_user", False)})
        held = [review_mod.review(self, m["id"], force=True) for m in self.list_memories(agent, status="quarantined")]
        self.ledger.audit(agent, "review.auto", None, {"conflicts": len(conflicts), "held": len(held)})
        return {"conflicts": conflicts, "held": held}

    def _user_present(self, agent: str) -> bool:
        """True for the UI, or when the user wrote to *this* agent very recently (its captured prompts).

        An agent can only speak for the user if the user was actually talking to it.
        """
        if agent == "ui":
            return True
        return bool(self.db.scalar(
            "SELECT 1 FROM events WHERE agent = ? AND role = 'user' AND source_type = 'user' AND created_at > ? LIMIT 1",
            (agent, now() - self.cfg.user_presence_window_min * 60),
        ))  # fmt: skip

    def _conflict_visible(self, agent: str, cid: str) -> list[dict]:
        mems = [self.ledger.get_memory(mid) for mid in json.loads(
            self.db.scalar("SELECT memory_ids FROM conflicts WHERE id = ?", (cid,)) or "[]")]  # fmt: skip
        mems = [m for m in mems if m]
        if not mems or not all(self._can_read_memory(agent, m) for m in mems):
            raise NotFound(f"conflict {cid!r} not found")
        return mems

    def pending_decisions(self, agent: str, limit: int = 20) -> list[dict]:
        """Conflicts the memory layer couldn't settle, for the calling agent to decide."""
        a = self.agent(agent)
        out = []
        for r in self.db.all("SELECT id FROM conflicts WHERE user_id = ? AND status = 'open' ORDER BY created_at LIMIT ?",
                             (a["user_id"], limit)):  # fmt: skip
            try:
                self._conflict_visible(agent, r["id"])
            except NotFound:
                continue
            out.append(resolve_mod.decision(self, r["id"]))
        return out

    def conflict_history(self, agent: str, limit: int = 30) -> list[dict]:
        """Conflicts already settled (by rules, RUFUR's model, an agent, the user or the default), newest first."""
        a = self.agent(agent)
        out = []
        for r in self.db.all("SELECT id FROM conflicts WHERE user_id = ? AND status != 'open' "
                             "ORDER BY coalesce(resolved_at, created_at) DESC LIMIT ?", (a["user_id"], limit)):  # fmt: skip
            c, mems = resolve_mod._load(self, r["id"])
            if not all(self._can_read_memory(agent, m) for m in mems):
                continue
            out.append({"conflict_id": c["id"], "kind": c["kind"], "status": c["status"],
                        "subject": " ".join(filter(None, [c["subject"], c["predicate"]])),
                        "resolution": c["resolution"], "created_at": c["created_at"], "resolved_at": c["resolved_at"],
                        "options": [{"memory_id": m["id"], "text": m["text"], "source": m["source_type"],
                                     "status": m["status"]} for m in mems]})  # fmt: skip
        return out

    def resolve_conflict(self, agent: str, conflict_id: str, *, action: str = "keep", winner: str | None = None,
                         scopes: dict | None = None, reason: str = "", decided_by: str = "agent") -> dict:  # fmt: skip
        """The calling agent's decision. decided_by="user" is believed only if the user wrote recently."""
        mems = self._conflict_visible(agent, conflict_id)
        if self.db.scalar("SELECT status FROM conflicts WHERE id = ?", (conflict_id,)) != "open":
            return resolve_mod.decision(self, conflict_id) | {"note": "already settled"}
        by = "user" if decided_by == "user" and self._user_present(agent) else "agent"
        if resolve_mod.needs_user(mems) and by != "user":
            # Letting an agent swap something the user said for a less trusted source is the planted-fact attack.
            if not (action == "keep" and winner and self.ledger.get_memory(winner)["trust"] == "high"):
                raise AccessDenied("this decision would replace something the user said with a less trusted source; "
                                   "ask the user and pass decided_by='user'")  # fmt: skip
        out = resolve_mod.apply(self, conflict_id, action=action, winner=winner, scopes=scopes, by=by,
                                reason=reason or f"decided by {agent}")  # fmt: skip
        self.ledger.audit(agent, "conflict.decide", conflict_id, {"by": by, "action": action})
        return out

    # ----------------------------------------------------------------- reads

    def recall(self, agent: str, query: str, *, token_budget: int | None = None, pool: str | None = None,
               session_id: str | None = None, threshold: float | None = None, verify: bool = True,
               exclude_sent: bool = False, record: bool = True) -> dict:  # fmt: skip
        """Working set → short-term → long-term. `exclude_sent` skips what this session already received."""
        started = time.perf_counter()
        query = self._as_user(query)
        ws = self.working_sets.get(session_id)
        if ws is not None and ws["agent"] != agent:
            ws = None
        if ws and not exclude_sent and threshold is None and (hits := ws_mod.lookup(self, ws, query, self.cfg.verify_band[1])):
            items = hits
            res = {"abstained": False, "missing": None, "items": items, "context": render(items),
                   "tokens": estimate_tokens(render(items)), "path": "working_set",
                   "latency_ms": round((time.perf_counter() - started) * 1000, 2), "used_vector": True}  # fmt: skip
        else:
            flt = self._filter(agent, pool=pool)

            def run() -> dict:
                return fast_recall(self.db, self.embedder, query, flt, token_budget=token_budget or self.cfg.default_token_budget,
                                   threshold=self.cfg.abstain_threshold if threshold is None else threshold,
                                   half_life_days=self.cfg.recency_half_life_days, k=self.cfg.candidate_k,
                                   verifier=self.verifier if (threshold is None and verify) else None,
                                   verify_band=tuple(self.cfg.verify_band),
                                   exclude=ws["sent"] if (ws and exclude_sent) else None,
                                   reranker=self.fast_reranker, rerank_top_n=self.cfg.rerank_top_n).as_dict()  # fmt: skip

            res = run()
            # A fading memory that answers this question is being used, so it comes back before we answer.
            if self._revive_fading(agent, query, pool):
                res = run()
        ids = [it["id"] for it in res["items"]]
        if ws is not None:
            ws["sent"].update(ids)
        res["decisions_needed"] = self._decisions_for(ids)
        if res["decisions_needed"]:
            res["context"] += "\n" + _render_decisions(res["decisions_needed"])
        stm_hits = [it for it in res["items"] if it.get("tier") == "stm"]
        stm_mod.extend_expiry(self, [it["id"] for it in stm_hits])
        if record:
            self.ledger.metric("recall", res["latency_ms"], path=res["path"], agent=agent,
                               tokens_in=estimate_tokens(query), tokens_out=res["tokens"],
                               meta={"abstained": res["abstained"], "items": len(ids), "query_hash": sha256(query)[:16],
                                     "vector": res["used_vector"],
                                     # Age of short-term items when reused; drives `rufur tune-stm`.
                                     "stm_ages_h": [round((now() - it["created_at"]) / 3600, 2) for it in stm_hits]})  # fmt: skip
            self.ledger.audit(agent, "recall", None, {"items": ids})
            self.bus.publish("recall", agent=agent, items=ids, abstained=res["abstained"])
        return res

    def _revive_fading(self, agent: str, query: str, pool: str | None) -> list[str]:
        flt = self._filter(agent, pool=pool, statuses=("archived",))
        if flt.empty:
            return []
        hits = fast_recall(self.db, self.embedder, query, flt, token_budget=400, threshold=self.cfg.abstain_threshold,
                           half_life_days=self.cfg.recency_half_life_days, k=5)  # fmt: skip
        return fade_mod.revive(self, [it["id"] for it in hits.items], agent)

    def _decisions_for(self, memory_ids: list[str]) -> list[dict]:
        if not memory_ids:
            return []
        want = set(memory_ids)
        return [resolve_mod.decision(self, r["id"]) for r in self.db.all(
            "SELECT id, memory_ids FROM conflicts WHERE status = 'open'") if want & set(json.loads(r["memory_ids"]))]  # fmt: skip

    def recall_past(self, agent: str, query: str, *, since: float | None = None, until: float | None = None,
                    include_history: bool = True, token_budget: int | None = None) -> dict:  # fmt: skip
        """Deep look into the past: superseded versions, archived items and daily journals, with time bounds.

        Every item carries its validity window, so old facts are labelled as old, not presented as current.
        """
        query = self._as_user(query)
        statuses = ("active", "disputed", "superseded", "archived") if include_history else ("active", "disputed")
        flt = self._filter(agent, statuses=statuses, current_only=False, since=since, until=until)
        out = fast_recall(self.db, self.embedder, query, flt, token_budget=token_budget or 1500,
                          threshold=self.cfg.abstain_threshold, half_life_days=3650.0, k=self.cfg.candidate_k * 2,
                          verifier=self.verifier, verify_band=tuple(self.cfg.verify_band),
                          reranker=self.fast_reranker, rerank_top_n=self.cfg.rerank_top_n,
                          header="Memories from the past. Items with until=... are no longer current. " + CONTEXT_HEADER)  # fmt: skip
        res = out.as_dict()
        res["path"] = "past"
        fade_mod.revive(self, [it["id"] for it in res["items"]], agent)
        self.ledger.metric("recall", res["latency_ms"], path="past", agent=agent, tokens_in=estimate_tokens(query),
                           tokens_out=res["tokens"], meta={"abstained": res["abstained"], "items": len(res["items"])})  # fmt: skip
        self.ledger.audit(agent, "recall_past", None, {"items": [it["id"] for it in res["items"]]})
        return res

    def bootstrap(self, agent: str, *, project: str | None = None, prompt: str | None = None,
                  session_id: str | None = None, pool: str | None = None) -> dict:  # fmt: skip
        """Start (or reuse) a session and preload its working set; the result is meant for context injection."""
        if not session_id:
            session_id = self.session_start(agent, pool=pool)["session_id"]
        out = ws_mod.build(self, agent, session_id, project, prompt)
        pending = self.pending_decisions(agent, limit=3)
        if pending:
            out["decisions_needed"] = pending
            out["context"] = "\n\n".join(filter(None, [out["context"], _render_decisions(pending)]))
        return out

    def _as_user(self, query: str) -> str:
        """Memories about the human are stored as "the user" (see WritePath._user_alias), so a question that
        names them ("Where does Asha live?") is asked the same way before searching."""
        for r in self.db.all("SELECT DISTINCT value FROM memories WHERE subject = 'user' "
                             "AND predicate IN ('name', 'full name', 'user name') AND status IN ('active', 'disputed')"):  # fmt: skip
            for name in {r["value"].strip(), r["value"].strip().split(" ")[0]}:
                if len(name) >= 2:
                    query = re.sub(rf"\b{re.escape(name)}('s)?\b", lambda m: "the user" + (m.group(1) or ""), query, flags=re.I)
        return query

    def _readable_memory(self, agent: str, memory_id: str) -> dict:
        m = self.ledger.get_memory(memory_id)
        # Same error whether it doesn't exist or the caller can't see it.
        if m is None or not self._can_read_memory(agent, m):
            raise NotFound(f"memory {memory_id!r} not found")
        return m

    def inspect(self, agent: str, memory_id: str) -> dict:
        m = self._readable_memory(agent, memory_id)
        m["sources"] = self.db.all(
            "SELECT s.event_id, s.quote, e.role, e.agent, e.source_type, e.created_at, e.session_id "
            "FROM memory_sources s LEFT JOIN events e ON e.id = s.event_id WHERE s.memory_id = ?",
            (memory_id,),
        )
        m["history"] = self._history(m)
        m["lineage"] = self.db.all("SELECT child_kind, child_id FROM lineage WHERE memory_id = ?", (memory_id,))
        m["related"] = [
            r for r in self.db.all(
                "SELECT id, text, status, trust, source_type, valid_from, valid_to FROM memories "
                "WHERE pool_id = ? AND subject = ? AND predicate = ? AND id != ? AND subject != ''",
                (m["pool_id"], m["subject"], m["predicate"], memory_id),
            )
        ]  # fmt: skip
        m["edges"] = self.db.all("SELECT src, relation, dst FROM edges WHERE memory_id = ?", (memory_id,))
        m["pool"] = self._pool(m["pool_id"])["name"]
        m["retention_score"] = retention_score(m, self.cfg.recency_half_life_days)
        if agent != "ui" and self.agent(agent)["clearance"] != "restricted":
            # Only the user's dashboard and agents cleared for restricted data see secrets; sources are masked too.
            m["text"], m["value"] = mask_secrets(m["text"]), mask_secrets(m["value"])
            for s in m["sources"]:
                s["quote"] = mask_secrets(s["quote"] or "")
        self.ledger.audit(agent, "inspect", memory_id)
        return m

    def _history(self, m: dict) -> list[dict]:
        """The supersede chain this memory belongs to, oldest first."""
        cols = "id, text, status, trust, valid_from, valid_to, superseded_by, source_type"
        chain = [self.db.one(f"SELECT {cols} FROM memories WHERE id = ?", (m["id"],))]
        cur = chain[0]
        while prev := self.db.one(f"SELECT {cols} FROM memories WHERE superseded_by = ?", (cur["id"],)):
            chain.insert(0, prev)
            cur = prev
        cur = chain[-1]
        while cur["superseded_by"] and (nxt := self.db.one(f"SELECT {cols} FROM memories WHERE id = ?", (cur["superseded_by"],))):
            chain.append(nxt)
            cur = nxt
        return chain

    # ---------------------------------------------------------------- forget

    def forget(self, agent: str, *, memory_id: str | None = None, memory_ids: list[str] | None = None,
               query: str | None = None, confirm: bool = False, include_history: bool = True,
               cascade_mentions: bool = False) -> dict:  # fmt: skip
        """By id(s): forget now. By query: return candidates and what would go with them, unless `confirm`
        (then forget all candidates). To forget only some candidates, pass their `memory_ids`.
        `cascade_mentions` also forgets the other memories that mention the forgotten value."""
        started = time.perf_counter()
        if memory_ids is not None and not memory_ids and not memory_id:
            return {"forgotten": [], "report": None, "error": "no memory ids given"}
        if memory_id or memory_ids:
            ids = [self._forgettable(agent, mid)["id"] for mid in ([memory_id] if memory_id else []) + (memory_ids or [])]
        elif query:
            flt = self._filter(agent, min_trust="low", statuses=("active", "disputed", "quarantined"))
            keyed = [r for r in (self._masked(agent, r) for r in
                                 forget_engine.key_match(self.db, query, flt.pool_ids + flt.stm_pool_ids)) if r]  # fmt: skip
            found = self._forget_candidates(agent, query, flt, keyed)
            candidates = found.pop("candidates")
            # With a judge, only what it decided to forget goes; without one, every candidate (the agent picks).
            chosen = [c for c in candidates if c.get("decision", "forget") == "forget"]
            if not confirm:
                pre = forget_engine.preview(self.db, [c["id"] for c in chosen], include_history)
                pre = {k: [x for x in (self._masked(agent, r) for r in v) if x] for k, v in pre.items()}
                return {"confirm_required": True, "candidates": candidates, "search": found, **pre,
                        "how_to_confirm": "call forget again with memory_ids=[...] for the ones the user means (or "
                                          "confirm=true to forget the ones marked decision=forget); add "
                                          "cascade_mentions=true if they also want also_mentioned_in gone"}  # fmt: skip
            ids = [c["id"] for c in chosen]
        else:
            raise ValueError("give memory_id, memory_ids or query")
        if not ids:
            return {"forgotten": [], "report": None}
        for mid in ids:
            m = self.ledger.get_memory(mid)
            if m and not self._can_write_pool(agent, m["pool_id"]):
                raise AccessDenied(f"agent {agent!r} cannot forget memories in this pool")
        out = forget_engine.forget(self.cfg, self.db, self.ledger, self.embedder, self.llm, ids, agent, include_history,
                                   cascade_mentions=cascade_mentions)  # fmt: skip
        self.ledger.metric("forget", (time.perf_counter() - started) * 1000, path="forget", agent=agent,
                           llm_tokens=(out.get("report") or {}).get("llm_tokens", 0),
                           meta={"count": len(out["forgotten"]),
                                 "completeness": (out.get("report") or {}).get("completeness")})  # fmt: skip
        return out

    def _forget_candidates(self, agent: str, query: str, flt: Filter, keyed: list[dict]) -> dict:
        """Qwen reranker + LLM judge when available; otherwise key matches plus a plain fast recall."""
        try:
            found = forget_engine.find_targets(self.cfg, self.db, self.embedder, self.llm, self.deep_reranker,
                                               query, flt, keyed)  # fmt: skip
            found["candidates"] = [x for x in (self._masked(agent, c) for c in found["candidates"]) if x]
            return found
        except RerankUnavailable as e:
            fallback = str(e)
        res = fast_recall(self.db, self.embedder, query, flt, token_budget=1500, threshold=self.cfg.abstain_threshold,
                          half_life_days=self.cfg.recency_half_life_days, k=self.cfg.candidate_k)  # fmt: skip
        candidates = [{**{k: r[k] for k in ("id", "text", "type", "status")}, "relevance": 1.0, "match": "key"}
                      for r in keyed]  # fmt: skip
        seen = {c["id"] for c in candidates}
        candidates += [{**{k: it[k] for k in ("id", "text", "type", "status", "relevance")}, "match": "semantic"}
                       for it in res.items if it["id"] not in seen]  # fmt: skip
        return {"candidates": candidates, "scope": "recall", "reranker": None, "judge": None, "note": fallback}

    def _masked(self, agent: str, r: dict) -> dict | None:
        """A forget candidate as this agent may see it: in full if it can read it; as an id with hidden text
        if it is above the agent's clearance but the user is talking to this agent (so they can ask for it
        to be forgotten without the agent ever reading it); otherwise not at all."""
        m = self.ledger.get_memory(r["id"])
        if m is None:
            return None
        if self._can_read_memory(agent, m):
            return r
        if self._user_present(agent) and self._can_write_pool(agent, m["pool_id"]):
            return {**r, "text": f"[{m['sensitivity']} memory, text hidden]"}
        return None

    def _forgettable(self, agent: str, memory_id: str) -> dict:
        """Readable memories, plus ones above the agent's clearance when the user asked this agent directly:
        a privacy request must not fail because the memory is too private for the agent to read."""
        m = self.ledger.get_memory(memory_id)
        if m and not self._can_read_memory(agent, m) and self._masked(agent, {"id": memory_id}):
            return m
        return self._readable_memory(agent, memory_id)

    def forget_receipts(self, agent: str, limit: int = 50) -> list[dict]:
        return receipts_mod.list_receipts(self.db, limit)

    def forget_receipt(self, agent: str, receipt_id: str) -> dict:
        r = receipts_mod.get(self.db, receipt_id)
        if r is None:
            raise NotFound(f"receipt {receipt_id!r} not found")
        return r

    def verify_forgetting(self, agent: str) -> dict:
        """Re-verify every deletion receipt now: chain, signatures, ids still absent, nothing resurfaced."""
        return receipts_mod.verify_chain(self.cfg, self.db)

    def _can_write_pool(self, agent: str, pool_id: str) -> bool:
        p = self._pool(pool_id)
        return _acl_allows(json.loads(p["acl"]), "write", agent, self.agent(agent)["user_id"])

    def _purge_quietly(self, memory_ids: list[str]) -> None:
        """Hard delete without a tombstone: routine expiry or a merge, not a user forget."""
        purge_memories(self.db, memory_ids)
        for mid in memory_ids:
            self.ledger.op("delete", mid, "system", {"reason": "expiry_or_merge"})
            self.bus.publish("memory.deleted", id=mid)

    def auto_forget(self, agent: str = "system") -> dict:
        """Move unused long-term memories to the fading tier; purge those that stayed unused there."""
        return fade_mod.fade(self, agent)

    # -------------------------------------------------------------- handover

    def handover_create(self, agent: str, task_id: str, packet: ho.HandoverIn) -> dict:
        p = packet.model_dump()
        if not p["memory_ids"]:
            # Gathering context for a task, not answering a question: a looser relevance bar is fine.
            res = self.recall(agent, packet.goal + " " + " ".join(packet.steps_pending), pool=packet.pool,
                              token_budget=1200, threshold=self.cfg.abstain_threshold / 2)  # fmt: skip
            p["memory_ids"] = [it["id"] for it in res["items"]]
        p["memory_ids"] = [mid for mid in p["memory_ids"] if self._safe_readable(agent, mid)]
        p["from_agent"] = agent
        p["created_at"] = now()
        return ho.save(self.db, self.ledger, task_id, p, agent)

    def _safe_readable(self, agent: str, memory_id: str) -> bool:
        m = self.ledger.get_memory(memory_id)
        return bool(m) and self._can_read_memory(agent, m)

    def handover_resume(self, agent: str, task_id: str, token_budget: int | None = None) -> dict:
        t = ho.get(self.db, task_id)
        if t is None:
            raise NotFound(f"task {task_id!r} not found")
        packet = t["packet"]
        visible = [mid for mid in packet.get("memory_ids", []) if self._safe_readable(agent, mid)]
        withheld = len(packet.get("memory_ids", [])) - len(visible)
        next_step = (packet.get("steps_pending") or [packet["goal"]])[0]
        context = self.recall(agent, f"{packet['goal']} {next_step}", token_budget=token_budget, pool=packet.get("pool"))
        ho.mark_resumed(self.db, self.ledger, task_id, agent)
        return {
            "task_id": task_id,
            "from_agent": t["from_agent"],
            "packet": {**packet, "memory_ids": visible},
            "memories": ho.provenance(self.db, visible),
            "withheld_memories": withheld,
            "next_step": next_step,
            "context": context,
        }

    def list_tasks(self, agent: str) -> list[dict]:
        tasks = self.db.all("SELECT * FROM tasks ORDER BY updated_at DESC")
        for t in tasks:
            t["packet"] = json.loads(t["packet"])
        return tasks

    # ------------------------------------------- consolidation, pools, STM

    def consolidate(self, agent: str, *, trigger: str = "agent", journal: bool = False) -> dict:
        """File the user's short-term memories into long-term stores now."""
        a = self.agent(agent)
        if a["user_id"].startswith("scratch:"):
            raise AccessDenied("subagents can't consolidate; close the pool instead")
        day = datetime.fromtimestamp(now()) if journal else None
        return consolidate_mod.consolidate(self, a["user_id"], trigger, journal_day=day)

    def _maybe_size_trigger(self, user_id: str) -> None:
        if user_id.startswith("scratch:"):
            return
        stm = self.stm_pool(user_id)["id"]
        pending = self.db.scalar(
            "SELECT count(*) FROM memories WHERE pool_id = ? AND tier = 'stm' AND consolidation_run IS NULL", (stm,)
        )
        last = self.db.scalar("SELECT max(started_at) FROM consolidation_runs WHERE user_id = ?", (user_id,)) or 0
        events = self.db.scalar("SELECT count(*) FROM events WHERE created_at > ?", (last,))
        if pending > self.cfg.size_trigger_items or events > self.cfg.size_trigger_events:
            self.worker.submit(consolidate_mod.consolidate, self, user_id, "size")

    def consolidation_runs(self, limit: int = 30) -> list[dict]:
        rows = self.db.all("SELECT * FROM consolidation_runs ORDER BY started_at DESC LIMIT ?", (limit,))
        for r in rows:
            r["stats"] = json.loads(r["stats"])
        return rows

    def pool_spawn(self, agent: str, purpose: str, *, needs: list[str] | None = None, memory_ids: list[str] | None = None,
                   ttl_minutes: int | None = None, session_id: str | None = None, seed_budget: int = 2000) -> dict:  # fmt: skip
        if self.agent(agent)["user_id"].startswith("scratch:"):
            raise AccessDenied("a subagent can't spawn further pools")
        return pools_mod.spawn(self, agent, purpose, needs or [], memory_ids or [], ttl_minutes, session_id, seed_budget)

    def _by_address(self, agent: str, address: str) -> tuple[dict, str]:
        try:
            return pools_mod.resolve(self, agent, address)
        except LookupError as e:
            raise NotFound(str(e)) from e

    def pool_search(self, agent: str, address: str, query: str, *, token_budget: int | None = None) -> dict:
        """Search only the pool behind a memory address: its seeded memories and the subagent's own writes."""
        p, sub = self._by_address(agent, address)
        owner = self.agent(p["owner_agent"])
        flt = Filter(pool_ids=[p["id"]], stm_pool_ids=[], member_pool_ids=[p["id"]],
                     default_target=self._pool(self.cfg.default_pool)["id"], clearance=owner["clearance"],
                     min_trust=self.cfg.min_trust)  # fmt: skip
        out = fast_recall(self.db, self.embedder, self._as_user(query), flt,
                          token_budget=token_budget or self.cfg.default_token_budget, threshold=self.cfg.abstain_threshold,
                          half_life_days=self.cfg.recency_half_life_days, k=self.cfg.candidate_k,
                          verifier=self.verifier, verify_band=tuple(self.cfg.verify_band),
                          reranker=self.fast_reranker, rerank_top_n=self.cfg.rerank_top_n,
                          header=f"Memories from pool {p['id']} ({p['purpose']}). " + CONTEXT_HEADER)  # fmt: skip
        res = out.as_dict()
        res["path"], res["pool"] = "pool", p["id"]
        if res["abstained"]:
            res["hint"] = "not in this pool; call pool_fetch with the same address to ask the memory layer for it"
        self.ledger.metric("recall", res["latency_ms"], path="pool", agent=agent, tokens_in=estimate_tokens(query),
                           tokens_out=res["tokens"], meta={"abstained": res["abstained"], "pool": p["id"]})  # fmt: skip
        self.ledger.audit(agent, "pool.search", p["id"], {"items": [it["id"] for it in res["items"]]})
        return res

    def pool_remember(self, agent: str, address: str, text: str, *, type: str | None = None, subject: str = "",
                      predicate: str = "", value: str = "") -> dict:  # fmt: skip
        """Write into the pool behind an address. It stays there until pool_close files the useful parts back."""
        p, sub = self._by_address(agent, address)
        return self.remember(sub, text, type=type, subject=subject, predicate=predicate, value=value, source="agent")

    def pool_fetch(self, agent: str, address: str, query: str, *, token_budget: int = 800) -> dict:
        """The subagent needs something the seed missed: the memory layer searches the parent's memory with the
        parent's permissions and adds the matches to the pool by reference, within a budget. Audited."""
        p, sub = self._by_address(agent, address)
        res = self.recall(p["owner_agent"], query, token_budget=token_budget, record=False)
        have = {r["memory_id"] for r in self.db.all("SELECT memory_id FROM pool_members WHERE pool_id = ?", (p["id"],))}
        added, spent = [], 0
        for it in [] if res["abstained"] else res["items"]:
            cost = estimate_tokens(it["text"])
            if it["id"] in have or spent + cost > token_budget:
                continue
            self.db.execute("INSERT OR IGNORE INTO pool_members(pool_id, memory_id) VALUES(?,?)", (p["id"], it["id"]))
            added.append({"id": it["id"], "text": it["text"]})
            spent += cost
        self.ledger.audit(agent, "pool.fetch", p["id"], {"added": [a["id"] for a in added]})
        return {"pool": p["id"], "added": added, "tokens": spent,
                "abstained": not added, "note": "now searchable with pool_search" if added else "the user's memory has nothing on this"}  # fmt: skip

    def pool_info(self, agent: str, address: str) -> dict:
        p, sub = self._by_address(agent, address)
        items = pools_mod.manifest(self, p["id"])
        return {"pool": p["id"], "purpose": p["purpose"], "owner": p["owner_agent"], "subagent": sub,
                "expires_at": p["expires_at"], "items": items,
                "tokens": sum(estimate_tokens(i["text"]) for i in items)}  # fmt: skip

    def pool_close(self, agent: str, pool_id: str, *, summary: str | None = None, reason: str = "closed") -> dict:
        if pool_id.startswith("mem://"):
            p, sub = self._by_address(agent, pool_id)
            pool_id = p["id"]
            if agent not in (p["owner_agent"], sub):
                agent = sub  # the address holder closes it as the subagent
        try:
            return pools_mod.close(self, agent, pool_id, summary, reason)
        except LookupError as e:
            raise NotFound(str(e)) from e
        except PermissionError as e:
            raise AccessDenied(str(e)) from e

    def scratch_pools(self, agent: str) -> list[dict]:
        return pools_mod.list_pools(self, agent)

    def subagent_stopped(self, agent: str, session_id: str | None, idle_s: float = 120.0) -> dict:
        """Close this agent's open scratch pools whose subagent has gone quiet (a hook safety net)."""
        closed = []
        for p in self.db.all("SELECT id FROM pools WHERE kind = 'scratch' AND closed_at IS NULL AND owner_agent = ?", (agent,)):
            sub = self.db.one("SELECT last_seen FROM agents WHERE user_id = ?", (f"scratch:{p['id']}",))
            if sub is None or (sub["last_seen"] or 0) < now() - idle_s:
                closed.append(self.pool_close(agent, p["id"], reason="subagent_stopped"))
        return {"closed": closed}

    def sweep(self) -> dict:
        out = stm_mod.sweep(self)
        out["decisions_defaulted"] = resolve_mod.apply_defaults(self)
        out["fading"] = fade_mod.fade(self)
        # Forgotten content that came back by a path that skipped the write-time tombstone check.
        back = receipts_mod.recheck(self.db)
        if back:
            forget_engine.forget(self.cfg, self.db, self.ledger, self.embedder, self.llm, back, "sweeper", verify=False)
            self.ledger.audit("sweeper", "forget.resurfaced", ",".join(back), {"count": len(back)})
        out["resurfaced_forgotten"] = len(back)
        return out

    def stm_stats(self) -> dict:
        out = stm_mod.suggest_ttl(self)
        out["items"] = {r["status"]: r["n"] for r in self.db.all(
            "SELECT status, count(*) AS n FROM memories WHERE tier = 'stm' GROUP BY status")}  # fmt: skip
        out["unconsolidated"] = self.db.scalar("SELECT count(*) FROM memories WHERE tier = 'stm' AND consolidation_run IS NULL")
        return out

    def _guard(self, agent: str) -> None:
        """Write-anomaly guard: a burst of writes or repeated attempts to override trusted facts look like a
        compromised or looping agent, so everything it wrote is held for review."""
        a = self.agent(agent)
        if a["quarantined"] or agent == "ui":
            return
        # Claims extracted from the user's own messages don't count: one pasted page legitimately yields dozens,
        # and that burst is what quarantined the user's main agent and held everything it wrote afterwards.
        recent = self.db.scalar("SELECT count(*) FROM memories WHERE author_agent = ? AND created_at > ? "
                                "AND source_type != 'user'", (agent, now() - 60))  # fmt: skip
        overrides = self.db.scalar(
            "SELECT count(*) FROM memories WHERE author_agent = ? AND created_at > ? "
            "AND (flags LIKE '%supersede_needs_confirmation%' OR flags LIKE '%conflicts_with%')",
            (agent, now() - 3600),
        )
        if recent > self.cfg.max_writes_per_min or overrides > self.cfg.max_override_attempts_per_h:
            self.quarantine_agent("guard", agent)
            self.ledger.audit("guard", "agent.auto_quarantine", agent, {"writes_per_min": recent, "overrides_per_h": overrides})

    def doctor(self) -> dict:
        """Consistency between SQL rows, vectors and lineage."""
        return {
            "memories_without_vector": self.db.scalar(
                "SELECT count(*) FROM memories WHERE id NOT IN (SELECT memory_id FROM vec_map WHERE kind = 'memory')"),
            "orphan_vectors": self.db.scalar("SELECT count(*) FROM vec_map WHERE memory_id NOT IN (SELECT id FROM memories)"),
            "orphan_lineage": self.db.scalar("SELECT count(*) FROM lineage WHERE memory_id NOT IN (SELECT id FROM memories)"),
            "memories_without_source": self.db.scalar(
                "SELECT count(*) FROM memories WHERE store != 'journal' AND id NOT IN (SELECT memory_id FROM memory_sources)"),
            "orphan_pool_members": self.db.scalar("SELECT count(*) FROM pool_members WHERE memory_id NOT IN (SELECT id FROM memories)"),
        }  # fmt: skip

    # ------------------------------------------------------- UI / inspection

    def list_memories(self, agent: str, *, pool: str | None = None, status: str | None = None, type: str | None = None,
                      tier: str | None = None, limit: int = 500) -> list[dict]:  # fmt: skip
        statuses = (status,) if status else ("active", "disputed", "quarantined", "superseded", "archived", "rejected")
        flt = self._filter(agent, pool=pool, statuses=statuses, min_trust="low", current_only=False)
        where, params = flt.sql()
        sql = f"SELECT m.*, p.name AS pool FROM memories m JOIN pools p ON p.id = m.pool_id WHERE {where}"
        if type:
            sql += " AND m.type = ?"
            params.append(type)
        if tier:
            sql += " AND m.tier = ?"
            params.append(tier)
        sql += " ORDER BY m.created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.db.all(sql, tuple(params))
        for r in rows:
            r["flags"] = json.loads(r["flags"])
        return rows

    def graph(self, agent: str, *, pool: str | None = None, limit: int = 500) -> dict:
        """Nodes: memories + entities + subjects. Links: relation, supersedes, about, source."""
        mems = self.list_memories(agent, pool=pool, limit=limit)
        ids = {m["id"] for m in mems}
        nodes: list[dict] = []
        links: list[dict] = []
        subjects: dict[str, str] = {}
        for m in mems:
            nodes.append({"id": m["id"], "kind": "memory", "label": m["text"][:80], "type": m["type"], "status": m["status"],
                          "trust": m["trust"], "importance": m["importance"], "pool": m["pool"], "created_at": m["created_at"],
                          "valid_to": m["valid_to"], "author_agent": m["author_agent"], "sensitivity": m["sensitivity"],
                          "tier": m["tier"], "store": m["store"]})  # fmt: skip
            if m["superseded_by"] in ids:
                links.append({"source": m["id"], "target": m["superseded_by"], "kind": "supersedes"})
            if m["subject"]:
                sid = subjects.setdefault(m["subject"], f"subj:{m['subject']}")
                links.append({"source": m["id"], "target": sid, "kind": "about"})
        for name, sid in subjects.items():
            nodes.append({"id": sid, "kind": "subject", "label": name})
        if ids:
            marks = ",".join("?" * len(ids))
            for e in self.db.all(f"SELECT * FROM edges WHERE memory_id IN ({marks})", tuple(ids)):
                for end in (e["src"], e["dst"]):
                    sid = f"subj:{end}"
                    if end not in subjects:
                        subjects[end] = sid
                        nodes.append({"id": sid, "kind": "entity", "label": end})
                links.append({"source": f"subj:{e['src']}", "target": f"subj:{e['dst']}", "kind": "relation",
                              "label": e["relation"], "memory_id": e["memory_id"]})  # fmt: skip
            # Memories that share a source event were learned together.
            rows = self.db.all(f"SELECT memory_id, event_id FROM memory_sources WHERE memory_id IN ({marks})", tuple(ids))
            by_event: dict[str, list[str]] = {}
            for r in rows:
                by_event.setdefault(r["event_id"], []).append(r["memory_id"])
            for group in by_event.values():
                for a, b in zip(group, group[1:]):
                    links.append({"source": a, "target": b, "kind": "co_source"})
        return {"nodes": nodes, "links": links}

    def quarantine(self, agent: str) -> list[dict]:
        rows = self.list_memories(agent, status="quarantined")
        for r in rows:
            r["sources"] = self.db.all("SELECT quote FROM memory_sources WHERE memory_id = ?", (r["id"],))
        return rows

    def metrics_summary(self) -> dict:
        def pct(values: list[float], q: float) -> float:
            if not values:
                return 0.0
            values = sorted(values)
            return round(values[min(len(values) - 1, int(q * len(values)))], 2)

        out: dict[str, Any] = {"ops": {}}
        for op in ("recall", "remember", "extract", "forget"):
            rows = self.db.all("SELECT latency_ms, tokens_in, tokens_out, llm_tokens, meta FROM metrics WHERE op = ?", (op,))
            lat = [r["latency_ms"] for r in rows]
            out["ops"][op] = {
                "count": len(rows),
                "p50_ms": pct(lat, 0.5),
                "p95_ms": pct(lat, 0.95),
                "tokens_out_avg": round(sum(r["tokens_out"] for r in rows) / len(rows), 1) if rows else 0,
                "llm_tokens": sum(r["llm_tokens"] for r in rows),
            }
        recalls = self.db.all("SELECT meta, tokens_out FROM metrics WHERE op = 'recall'")
        out["abstentions"] = sum(1 for r in recalls if json.loads(r["meta"]).get("abstained"))
        # Baseline: what stuffing the full event history into every prompt would have cost.
        history_tokens = sum(estimate_tokens(r["content"]) for r in self.db.all("SELECT content FROM events"))
        out["full_history_tokens"] = history_tokens
        out["avg_recall_tokens"] = out["ops"]["recall"]["tokens_out_avg"]
        out["token_savings_pct"] = (
            round(100 * (1 - out["avg_recall_tokens"] / history_tokens), 1) if history_tokens and recalls else None
        )
        out["counts"] = {
            r["status"]: r["n"] for r in self.db.all("SELECT status, count(*) AS n FROM memories GROUP BY status")
        }
        out["events"] = self.db.scalar("SELECT count(*) FROM events")
        out["tombstones"] = self.db.scalar("SELECT count(*) FROM tombstones")
        out["series"] = self.db.all(
            "SELECT op, created_at, latency_ms, tokens_out FROM metrics ORDER BY id DESC LIMIT 200"
        )
        return out

    def dashboard_sessions(self, agent: str) -> dict:
        return dash_mod.sessions(self, agent)

    def dashboard_memories(self, agent: str, kind: str, ident: str) -> dict:
        try:
            return dash_mod.memories(self, agent, kind, ident)
        except LookupError as e:
            raise NotFound(str(e)) from e

    def dashboard_timeline(self, agent: str, kind: str, ident: str) -> dict:
        try:
            return dash_mod.timeline(self, agent, kind, ident)
        except LookupError as e:
            raise NotFound(str(e)) from e

    def dashboard_tokens(self, bucket: str = "session") -> dict:
        return dash_mod.tokens(self, bucket)

    def dashboard_forgetting(self, agent: str) -> dict:
        return dash_mod.forgetting(self, agent)

    def _raw_guard(self, agent: str) -> None:
        # The raw view bypasses pool ACLs and clearance, so only the human's UI identity may use it.
        if agent != "ui":
            raise AccessDenied("the raw database view is only available to the UI")

    def raw_catalog(self, agent: str) -> dict:
        self._raw_guard(agent)
        return raw_mod.catalog(self)

    def raw_rows(self, agent: str, table: str, **kw) -> dict:
        self._raw_guard(agent)
        try:
            return raw_mod.rows(self, table, **kw)
        except LookupError as e:
            raise NotFound(str(e)) from e

    def audit_log(self, limit: int = 200) -> list[dict]:
        return self.db.all("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))

    def event(self, agent: str, event_id: str) -> dict:
        ev = self.db.one("SELECT * FROM events WHERE id = ?", (event_id,))
        if ev is None or (ev["pool_id"] not in self.readable_pools(agent) and ev["agent"] != agent):
            raise NotFound(f"event {event_id!r} not found")
        return ev

    def reconcile_embeddings(self) -> int:
        """Embed memories that were written while the embedder was down."""
        rows = self.db.all(
            "SELECT id, text FROM memories WHERE id NOT IN (SELECT memory_id FROM vec_map WHERE kind = 'memory')"
        )
        done = 0
        for i in range(0, len(rows), 32):
            batch = rows[i : i + 32]
            try:
                vecs = self.embedder.embed([r["text"] for r in batch])
            except EmbedUnavailable:
                break
            for r, v in zip(batch, vecs):
                self.ledger.index_vector("memory", r["id"], r["id"], v)
                done += 1
        return done

    def health(self) -> dict:
        ollama = {"reachable": False, "models": []}
        try:
            import httpx

            r = httpx.get(f"{self.cfg.ollama_url}/api/tags", timeout=2)
            ollama = {"reachable": True, "models": [m["name"] for m in r.json().get("models", [])]}
        except Exception:
            pass
        return {
            "status": "ok",
            "version": __import__("rufur").__version__,
            "home": str(self.cfg.home),
            "memories": self.db.scalar("SELECT count(*) FROM memories"),
            "embedder": self.embedder.name,
            # Recall falls back to no reranking if a model fails to load, so say which one is really in use.
            "rerankers": {role: {"model": r.model_name, "state": r.state} if r is not None else {"model": "none", "state": "off"}
                          for role, r in (("recall", self.fast_reranker), ("forget", self.deep_reranker))},  # fmt: skip
            "offline": self.cfg.offline,
            "ollama": ollama,
        }


def _claim_in(claim) -> ClaimIn:
    return ClaimIn(
        type=claim.type,
        text=claim.text,
        subject=claim.subject,
        predicate=claim.predicate,
        value=claim.value,
        quote=claim.quote,
        sensitivity=claim.sensitivity,
        relation=claim.relation.model_dump() if claim.relation else None,
        table_row=claim.table_row.model_dump() if claim.table_row else None,
    )


def _render_decisions(decisions: list[dict]) -> str:
    """Open conflicts as a block the calling agent can act on."""
    lines = ['<decisions-needed note="RUFUR could not settle these. Settle each one now with resolve_conflict: decide '
             'from the conversation if you can; if needs_user is true, ask the user one short question and pass '
             'decided_by=user; otherwise keep the recommended one.">']
    for d in decisions:
        opts = " | ".join(f'{o["memory_id"]}: "{o["text"]}" ({o["source"]}, {o["trust"]} trust)' for o in d["options"])
        lines.append(f'<decision id="{d["conflict_id"]}" needs_user="{str(d["needs_user"]).lower()}" '
                     f'recommended="{d["recommendation"]["memory_id"]}">{d["question"]} {opts}</decision>')
    lines.append("</decisions-needed>")
    return "\n".join(lines)
