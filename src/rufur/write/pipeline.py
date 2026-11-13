"""The write path: trust → tombstone check → conflict engine → placement.

`commit` turns one validated claim into ledger state. New memories land in the
user's short-term tier; consolidation (write/consolidate.py) later promotes
them in place into long-term stores.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any

from ..config import Config
from ..db.conn import Database, dumps
from ..embed import EmbedUnavailable
from ..forget import tombstone
from ..ledger import Ledger
from ..llm import LLM
from ..textutil import norm_key, now
from . import conflict, resolve
from .importance import initial_importance
from .trust import TRUST_ORDER, hold_reason, initial_status, instruction_like, trust_at_least, trust_for

CHUNK_CHARS = 900


@dataclass
class ClaimIn:
    type: str
    text: str
    subject: str = ""
    predicate: str = ""
    value: str = ""
    quote: str = ""
    sensitivity: str = "internal"
    relation: dict | None = None  # {src, relation, dst}
    table_row: dict | None = None  # {table, row}
    pinned: bool = False


@dataclass
class CommitResult:
    memory: dict | None
    outcome: str  # created | duplicate | corroborated | tombstoned
    related: list[dict] = field(default_factory=list)  # [{id, outcome, reason}]
    llm_tokens: int = 0
    conflict_id: str | None = None  # set when this write disagrees with an existing memory

    def summary(self) -> dict[str, Any]:
        m = self.memory
        return {
            "outcome": self.outcome,
            "memory": None if m is None else {k: m[k] for k in ("id", "status", "trust", "type", "text")},
            "related": self.related,
            "conflict_id": self.conflict_id,
        }


class WritePath:
    def __init__(self, cfg: Config, db: Database, ledger: Ledger, llm: LLM, embedder):
        self.cfg, self.db, self.ledger, self.llm, self.embedder = cfg, db, ledger, llm, embedder

    # -- helpers -----------------------------------------------------------

    def _embed(self, text: str) -> list[float] | None:
        try:
            return self.embedder.embed([text])[0]
        except EmbedUnavailable:
            return None

    def _user_alias(self, subject: str, pools: list[str]) -> bool:
        """True if `subject` is the user's own name ("Rahul Sharma" or just "Rahul"), so it means "user"."""
        key = norm_key(subject)
        if not key or key == "user":
            return False
        marks = ",".join("?" * len(pools))
        for r in self.db.all(
            f"SELECT value FROM memories WHERE pool_id IN ({marks}) AND subject = 'user' "
            "AND predicate IN ('name', 'full name', 'user name') AND status IN ('active', 'disputed')",
            tuple(pools),
        ):
            name = norm_key(r["value"])
            if key == name or (name and key == name.split()[0]):
                return True
        return False

    def _trust(self, source_type: str, agent: dict) -> str:
        if source_type == "agent":
            return agent["trust"]
        return trust_for(source_type)

    def _place(self, mem: dict, claim: ClaimIn, vec: list[float] | None) -> None:
        """Route derived views by memory type (plan §5.6)."""
        if vec is not None:
            self.ledger.index_vector("memory", mem["id"], mem["id"], vec)
        if claim.type == "relation" and claim.relation:
            r = claim.relation
            self.ledger.add_edge(norm_key(r["src"]), norm_key(r["relation"]), norm_key(r["dst"]), mem["id"], mem["pool_id"])
        if claim.type == "table" and claim.table_row:
            self.ledger.add_typed_row(norm_key(claim.table_row["table"]).replace(" ", "_"), claim.table_row["row"], mem["id"], mem["pool_id"])
        if claim.type in ("procedure", "document"):
            self.add_document(mem, claim.value if len(claim.value) > len(claim.text) else claim.text)

    def add_document(self, mem: dict, body: str) -> None:
        """Markdown file + chunks + chunk vectors, all linked to the memory by lineage."""
        path = os.path.join(self.cfg.files_dir, mem["pool_id"], f"{mem['id']}.md")
        self.ledger.add_file(path, f"# {mem['subject'] or mem['predicate'] or mem['id']}\n\n{body}\n", mem["id"])
        chunks = [body[i : i + CHUNK_CHARS] for i in range(0, len(body), CHUNK_CHARS)]
        ids = [self.ledger.add_chunk(mem["id"], path, i, c) for i, c in enumerate(chunks)]
        try:
            for cid, cvec in zip(ids, self.embedder.embed(chunks)):
                self.ledger.index_vector("file_chunk", cid, mem["id"], cvec)
        except EmbedUnavailable:
            pass

    def _insert(self, claim: ClaimIn, *, pool_id: str, agent: dict, source_type: str, trust: str,
                status: str, flags: list[str], session_id: str | None, scope: str | None = None,
                tier: str = "stm", target_pool: str | None = None) -> dict:  # fmt: skip
        t = now()
        volatility = "volatile" if claim.type == "event" else "slow"
        return self.ledger.insert_memory(
            {
                "tier": tier,
                "target_pool": target_pool,
                "expires_at": t + self.cfg.stm_base_ttl_h * 3600 if tier == "stm" else None,
                "volatility": volatility,
                "revalidate_at": t + (7 if volatility == "volatile" else 180) * 86400,
                "pool_id": pool_id,
                "session_id": session_id,
                "type": claim.type,
                "subject": norm_key(claim.subject),
                "predicate": norm_key(claim.predicate),
                "value": claim.value,
                "text": claim.text,
                "scope": scope,
                "status": status,
                "trust": trust,
                "importance": initial_importance(claim.type, claim.text),
                "sensitivity": claim.sensitivity,
                "owner": agent["name"],
                "author_agent": agent["name"],
                "source_type": source_type,
                "flags": flags,
                "pinned": int(claim.pinned),
                "status_reason": hold_reason(trust, flags) if status == "quarantined" else None,
            }
        )

    # -- commit ----------------------------------------------------------------

    def commit(self, claim: ClaimIn, *, pool_id: str, agent: dict, source_type: str,
               sources: list[tuple[str, str]], session_id: str | None = None, tier: str = "stm",
               target_pool: str | None = None, candidate_pools: list[str] | None = None) -> CommitResult:  # fmt: skip
        """`sources` is [(event_id, quote)]; quotes have already been validated.

        `candidate_pools` are where conflicts are looked for: for an STM write, the user's STM plus
        the long-term pool it will be promoted into.
        """
        if self._user_alias(claim.subject, candidate_pools or [pool_id]):
            # One person, one key space: otherwise "rahul sharma|age" and "user|age" never meet as a key match
            # and only collide as fuzzy vector neighbours.
            claim = replace(claim, subject="user")
        if tombstone.is_tombstoned(self.db, claim.subject, claim.predicate, claim.value, claim.text):
            self.ledger.audit(agent["name"], "write.blocked_tombstone", None)
            return CommitResult(None, "tombstoned")

        trust = self._trust(source_type, agent)
        flags: list[str] = []
        if instruction_like(claim.text) or any(instruction_like(q) for _, q in sources):
            flags.append("instruction_like")
        if agent.get("quarantined"):
            flags.append("agent_quarantined")
        status = "quarantined" if agent.get("quarantined") else initial_status(trust, flags)

        vec = self._embed(claim.text)
        candidates = conflict.find_candidates(self.db, candidate_pools or [pool_id], claim.subject, claim.predicate, vec,
                                             radius=self.cfg.conflict_radius)  # fmt: skip
        related: list[dict] = []
        llm_tokens = 0
        scope_new: str | None = None
        to_supersede: list[dict] = []
        to_dispute: list[dict] = []

        # Judge change signals on what the source actually said, not on the extractor's rewording.
        evidence = " ".join(
            (self.db.scalar("SELECT content FROM events WHERE id = ?", (eid,)) or q) for eid, q in sources
        )
        for existing, how in candidates:
            verdict, tokens = conflict.classify(self.llm, existing, claim.text, how, claim.value, evidence, claim.subject,
                                                claim.predicate, source_quote=" ".join(q for _, q in sources)[:500],
                                                margin=self.cfg.decision_margin)  # fmt: skip
            llm_tokens += tokens
            if verdict.outcome == "duplicate":
                return self._corroborate(existing, claim, agent, source_type, trust, sources, llm_tokens)
            if verdict.outcome == "unrelated":
                continue
            if verdict.outcome == "coexist":
                related.append({"id": existing["id"], "outcome": "coexist", "reason": verdict.reason})
                continue
            related.append({"id": existing["id"], "outcome": verdict.outcome, "reason": verdict.reason})
            if verdict.outcome == "update":
                if existing["trust"] == "high" and not trust_at_least(trust, "high"):
                    # A lower-trust source can't silently overwrite a high-trust fact.
                    flags.append(f"supersede_needs_confirmation:{existing['id']}")
                    status = "quarantined"
                else:
                    to_supersede.append(existing)
            elif verdict.outcome == "context":
                scope_new = verdict.scope_new
                if verdict.scope_old and not existing["scope"]:
                    self.ledger.update_memory(existing["id"], scope=verdict.scope_old)
            elif verdict.outcome == "unclear":
                # Rules settle what they can before falling back to "disputed".
                old_rank, new_rank = TRUST_ORDER.index(existing["trust"]), TRUST_ORDER.index(trust)
                confirmed = "user_confirmed" in str(existing["flags"])
                if confirmed or new_rank < old_rank:
                    # The user's own confirmation, or a more trusted source, wins; hold the newcomer for review.
                    flags.append(f"conflicts_with:{existing['id']}")
                    status = "quarantined"
                elif new_rank > old_rank:
                    to_supersede.append(existing)
                else:
                    to_dispute.append(existing)

        if to_dispute and status == "active":
            status = "disputed"
        mem = self._insert(claim, pool_id=pool_id, agent=agent, source_type=source_type, trust=trust, status=status,
                           flags=flags, session_id=session_id, scope=scope_new, tier=tier,
                           target_pool=target_pool)  # fmt: skip
        for event_id, quote in sources:
            self.ledger.add_source(mem["id"], event_id, quote)
        self._place(mem, claim, vec)

        if status != "quarantined":
            for old in to_supersede:
                self.ledger.update_memory(old["id"], status="superseded", valid_to=now(), superseded_by=mem["id"])
                self.ledger.op("supersede", old["id"], agent["name"], {"by": mem["id"]})
            for old in to_dispute:
                if old["status"] == "active":
                    self.ledger.update_memory(old["id"], status="disputed")
                    self.ledger.op("dispute", old["id"], agent["name"], {"with": mem["id"]})
        if status == "quarantined":
            self.ledger.op("quarantine", mem["id"], agent["name"], {"flags": flags})
        self.ledger.audit(agent["name"], "write", mem["id"], {"status": status, "pool": pool_id})
        # Disagreements the rules above couldn't settle become a conflict for the resolver.
        challenged = [f.split(":", 1)[1] for f in flags if f.startswith(("supersede_needs_confirmation:", "conflicts_with:"))]
        conflict_id = None
        if challenged:
            conflict_id = resolve.open_conflict(self.db, self.ledger, "override", mem, challenged, agent["user_id"])
        elif to_dispute and status != "quarantined":
            conflict_id = resolve.open_conflict(self.db, self.ledger, "dispute", mem, [o["id"] for o in to_dispute],
                                                agent["user_id"])  # fmt: skip
        elif to_dispute:
            # A quarantined newcomer disputing a live memory is an override attempt.
            conflict_id = resolve.open_conflict(self.db, self.ledger, "override", mem, [o["id"] for o in to_dispute],
                                                agent["user_id"])  # fmt: skip
        return CommitResult(mem, "created", related, llm_tokens, conflict_id)

    def _corroborate(self, existing: dict, claim: ClaimIn, agent: dict, source_type: str, trust: str,
                     sources: list[tuple[str, str]], llm_tokens: int) -> CommitResult:  # fmt: skip
        """Same claim seen again: attach the source and maybe lift quarantine."""
        # Only a new agent or source type counts as independent support; repeating yourself doesn't.
        # Checked before attaching the new source, which would otherwise always match.
        seen_before = self.db.scalar(
            "SELECT 1 FROM memory_sources s JOIN events e ON e.id = s.event_id "
            "WHERE s.memory_id = ? AND e.agent = ? AND e.source_type = ? LIMIT 1",
            (existing["id"], agent["name"], source_type),
        )
        for event_id, quote in sources:
            self.ledger.add_source(existing["id"], event_id, quote)
        updates: dict[str, Any] = {"confidence": min(0.99, existing["confidence"] + 0.1)}
        if not seen_before:
            updates["corroboration"] = (existing.get("corroboration") or 1) + 1
        outcome = "duplicate"
        if existing["status"] == "quarantined":
            independent = source_type != existing["source_type"] or agent["name"] != existing["author_agent"]
            # Agreement alone never lifts a claim that contradicts a trusted memory; that is the resolver's call.
            flagged = (
                "instruction_like" in str(existing["flags"]) or instruction_like(claim.text)
                or "conflicts_with:" in str(existing["flags"]) or "supersede_needs_confirmation:" in str(existing["flags"])
            )  # fmt: skip
            if not flagged and trust != "low" and independent:
                updates["status"] = "active"
                updates["trust"] = max(existing["trust"], trust, key=TRUST_ORDER.index)
                outcome = "corroborated"
                self.ledger.op("confirm", existing["id"], agent["name"], {"by": "corroboration"})
        self.ledger.update_memory(existing["id"], **updates)
        return CommitResult(self.ledger.get_memory(existing["id"]), outcome, [], llm_tokens)
