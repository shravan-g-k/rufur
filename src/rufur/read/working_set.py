"""The working set: memories preloaded into a session before the first prompt.

Built at session start from the user's profile, the last day of short-term
memory and the long-term items most relevant to the project. It is injected
into the agent's context by the SessionStart hook, and kept in RAM (with
embeddings) so later recalls in that session can be answered without a search
when one of these items clearly matches.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from typing import TYPE_CHECKING

from ..embed import EmbedUnavailable
from ..textutil import content_words, estimate_tokens, now
from .recall import CONTEXT_HEADER, coverage, item_of, render
from .search import Candidate

if TYPE_CHECKING:
    from ..service import MemoryService

WS_HEADER = (
    "Working memory loaded for this session (profile, recent context, project-relevant facts). "
    + CONTEXT_HEADER
)


class WorkingSets:
    """Small LRU of per-session working sets."""

    def __init__(self, capacity: int = 64):
        self.capacity = capacity
        self._sets: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, key: str, ws: dict) -> None:
        with self._lock:
            self._sets[key] = ws
            self._sets.move_to_end(key)
            while len(self._sets) > self.capacity:
                self._sets.popitem(last=False)

    def get(self, key: str | None) -> dict | None:
        if not key:
            return None
        with self._lock:
            ws = self._sets.get(key)
            if ws:
                self._sets.move_to_end(key)
            return ws


def build(svc: "MemoryService", agent: str, session_id: str, project: str | None, prompt: str | None) -> dict:
    flt = svc._filter(agent)
    where, params = flt.sql()
    budget = svc.cfg.working_set_budget
    chosen: dict[str, dict] = {}

    def take(rows: list[dict]) -> None:
        for m in rows:
            if m["id"] not in chosen:
                chosen[m["id"]] = m

    # 1. Profile: stable facts about the user, always worth having.
    take(svc.db.all(f"SELECT m.* FROM memories m WHERE {where} AND m.store = 'profile' ORDER BY m.importance DESC LIMIT 20", tuple(params)))
    # 2. What was discussed in the last day, from any of this user's agents.
    take(svc.db.all(
        f"SELECT m.* FROM memories m WHERE {where} AND m.tier = 'stm' AND m.created_at > ? ORDER BY m.created_at DESC LIMIT 15",
        (*params, now() - 86400),
    ))  # fmt: skip
    # 3. Long-term items relevant to the project and the opening prompt.
    query = " ".join(filter(None, [project, prompt]))
    if query.strip():
        res = svc.recall(agent, query, token_budget=budget, threshold=svc.cfg.abstain_threshold / 2, verify=False,
                         record=False)  # fmt: skip
        ids = [it["id"] for it in res["items"]]
        if ids:
            take(svc.db.all(f"SELECT * FROM memories WHERE id IN ({','.join('?' * len(ids))})", tuple(ids)))

    items, used = [], estimate_tokens(WS_HEADER) + 10
    # Visibility (pool ACLs, clearance, status, trust) is already enforced by the filter above.
    for m in chosen.values():
        cost = estimate_tokens(m["text"]) + 25
        if items and used + cost > budget:
            continue
        used += cost
        items.append(item_of(m, m["text"], m["importance"], 1.0, clearance=flt.clearance))
    try:
        vecs = svc.embedder.embed([it["text"] for it in items]) if items else []
    except EmbedUnavailable:
        vecs = []
    ws = {"items": items, "vecs": vecs, "sent": {it["id"] for it in items}, "built_at": now(), "agent": agent}
    svc.working_sets.put(session_id, ws)
    return {"session_id": session_id, "context": render(items, WS_HEADER) if items else "", "items": items,
            "tokens": used if items else 0}  # fmt: skip


def lookup(svc: "MemoryService", ws: dict, query: str, high: float) -> list[dict] | None:
    """Answer from the working set when one of its items clearly matches; None means 'go search'."""
    if not ws["items"] or not ws["vecs"]:
        return None
    try:
        qv = svc.embedder.embed([query], kind="query")[0]
    except EmbedUnavailable:
        return None
    qwords = content_words(query)
    scored = []
    for it, v in zip(ws["items"], ws["vecs"]):
        cos = sum(a * b for a, b in zip(qv, v))
        cand = Candidate(memory={**it, "subject": "", "predicate": "", "value": "", "scope": it["scope"]}, cosine=cos)
        c = min(1.0, max(0.0, (cos - svc.embedder.cos_floor) / svc.embedder.cos_span))
        rel = 0.5 * c + 0.5 * coverage(qwords, cand)
        scored.append((rel, it))
    scored.sort(key=lambda t: t[0], reverse=True)
    top = scored[0][0]
    if top < high or math.isnan(top):
        return None
    hits = [dict(it, relevance=round(rel, 4)) for rel, it in scored if rel >= top * 0.7]
    # Drop anything changed since the set was built (forgotten, superseded, quarantined).
    ids = [h["id"] for h in hits]
    live = {r["id"] for r in svc.db.all(
        f"SELECT id FROM memories WHERE id IN ({','.join('?' * len(ids))}) AND status IN ('active', 'disputed') AND valid_to IS NULL",
        tuple(ids))}  # fmt: skip
    hits = [h for h in hits if h["id"] in live]
    return hits or None
