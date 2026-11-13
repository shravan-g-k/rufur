"""Intelligent forgetting: long-term memories nobody uses fade out instead of piling up.

1. Fade: an active long-term memory that hasn't been recalled for `fade_after_days` and whose retention
   score (importance, recency of use, use count) is below `fade_below` moves to the fading tier: status
   `archived` with the `fading` flag. Normal recall stops showing it; recall_past and the Forgetting
   page still do.
2. Revive: every recall also looks in the fading tier. A fading memory that matches a question is used
   again, so it goes straight back to active, with its use counted.
3. Purge: a memory that stays fading for `fade_purge_days` without being revived is hard-deleted. It is
   routine decay, not a user's request, so no tombstone: the fact may be learned again later.

Never faded: pinned memories, decisions, anything the user confirmed, the user profile and procedures.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..textutil import now
from ..write.importance import retention_score

if TYPE_CHECKING:
    from ..service import MemoryService

FADING = "fading"
PROTECTED_STORES = ("profile", "procedure")


def fade(svc: "MemoryService", agent: str = "sweeper") -> dict:
    c, t = svc.cfg, now()
    faded, purged = [], []
    for m in svc.db.all(
        f"SELECT * FROM memories WHERE tier = 'ltm' AND status = 'active' AND pinned = 0 AND type != 'decision' "
        f"AND store NOT IN ({','.join('?' * len(PROTECTED_STORES))}) AND flags NOT LIKE '%user_confirmed%' "
        f"AND coalesce(last_accessed, created_at) < ?",
        (*PROTECTED_STORES, t - c.fade_after_days * 86400),
    ):  # fmt: skip
        score = retention_score(m, c.recency_half_life_days)
        if score < c.fade_below:
            current = svc.ledger.get_memory(m["id"])["flags"]
            svc.ledger.update_memory(m["id"], status="archived", flags=sorted(set(current) | {FADING}))
            svc.ledger.op("archive", m["id"], agent, {"reason": "unused", "retention": score})
            faded.append(m["id"])
    stale = [r["id"] for r in svc.db.all(
        "SELECT id FROM memories WHERE tier = 'ltm' AND status = 'archived' AND flags LIKE ? AND updated_at < ?",
        (f'%"{FADING}"%', t - c.fade_purge_days * 86400),
    )]  # fmt: skip
    if stale:
        svc._purge_quietly(stale)
        purged = stale
    if faded or purged:
        svc.ledger.audit(agent, "memory.fade", None, {"faded": len(faded), "purged": len(purged)})
    return {"faded": faded, "purged": purged}


def revive(svc: "MemoryService", memory_ids: list[str], agent: str) -> list[str]:
    """Fading memories that were just used go back to active."""
    back = []
    for mid in memory_ids:
        m = svc.ledger.get_memory(mid)
        if m and m["status"] == "archived" and FADING in m["flags"]:
            svc.ledger.update_memory(mid, status="active", flags=[f for f in m["flags"] if f != FADING])
            svc.db.execute("UPDATE memories SET access_count = access_count + 1, last_accessed = ? WHERE id = ?", (now(), mid))
            svc.ledger.op("revive", mid, agent, {"reason": "used while fading"})
            back.append(mid)
    return back


def fading(svc: "MemoryService") -> list[dict]:
    """The fading tier, soonest purge first, for the dashboard."""
    rows = svc.db.all(
        "SELECT id, text, type, store, importance, access_count, last_accessed, created_at, updated_at FROM memories "
        "WHERE tier = 'ltm' AND status = 'archived' AND flags LIKE ? ORDER BY updated_at",
        (f'%"{FADING}"%',),
    )
    for r in rows:
        r["purge_at"] = r["updated_at"] + svc.cfg.fade_purge_days * 86400
    return rows
