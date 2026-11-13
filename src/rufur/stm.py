"""Short-term memory lifecycle: sliding expiry, the sweeper, and data-driven TTL tuning.

An STM item lives `stm_base_ttl_h` from creation; every use keeps it alive at least
`stm_extend_h` past that use, never beyond `stm_max_ttl_h` from creation. Expiry
never silently drops something nobody reviewed: unconsolidated items are sent
through consolidation first, and only what consolidation discarded (or what sat in
quarantine) is archived, then hard-deleted after a grace period.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import TYPE_CHECKING

from .textutil import now

if TYPE_CHECKING:
    from .service import MemoryService


def extend_expiry(svc: "MemoryService", memory_ids: list[str]) -> None:
    if not memory_ids:
        return
    c = svc.cfg
    marks = ",".join("?" * len(memory_ids))
    svc.db.execute(
        f"""UPDATE memories SET expires_at = MIN(created_at + ?, MAX(created_at + ?, ?))
            WHERE tier = 'stm' AND id IN ({marks})""",
        (c.stm_max_ttl_h * 3600, c.stm_base_ttl_h * 3600, now() + c.stm_extend_h * 3600, *memory_ids),
    )


def sweep(svc: "MemoryService") -> dict:
    from .write.consolidate import consolidate

    t = now()
    stats: dict = {"idle_sessions_extracted": 0, "consolidated": 0, "archived": 0, "purged": 0, "pools_closed": 0,
                   "events_dropped": 0}  # fmt: skip

    # 0. Sessions that went quiet with unprocessed messages (clients often just quit without ending one).
    for r in svc.db.all(
        """SELECT s.id FROM sessions s WHERE s.ended_at IS NULL AND EXISTS (
               SELECT 1 FROM events e WHERE e.session_id = s.id AND e.extracted = 0)
           AND (SELECT max(created_at) FROM events e WHERE e.session_id = s.id) < ?""",
        (t - svc.cfg.session_idle_extract_min * 60,),
    ):
        svc.extract_session(r["id"])
        stats["idle_sessions_extracted"] += 1

    # 1. Expiring items nobody has filed yet get consolidated first (LLM, or rules if it's down).
    pending = svc.db.all(
        """SELECT m.id, p.name AS pool FROM memories m JOIN pools p ON p.id = m.pool_id
           WHERE m.tier = 'stm' AND p.kind = 'stm' AND m.consolidation_run IS NULL
             AND m.status IN ('active', 'disputed') AND m.expires_at < ?""",
        (t,),
    )
    by_user: dict[str, list[str]] = defaultdict(list)
    for r in pending:
        by_user[r["pool"].split(":", 1)[1]].append(r["id"])
    for user, ids in by_user.items():
        stats["consolidated"] += consolidate(svc, user, "expiry", memory_ids=ids)["items"]

    # 2. Archive what consolidation discarded, and quarantined items nobody reviewed in time.
    expired = svc.db.all(
        """SELECT id FROM memories WHERE tier = 'stm' AND pinned = 0 AND expires_at < ?
           AND ((status = 'active' AND store = 'discard') OR status = 'quarantined')""",
        (t,),
    )
    for r in expired:
        svc.ledger.update_memory(r["id"], status="archived")
        svc.ledger.op("archive", r["id"], "sweeper", {"reason": "stm_expired"})
    stats["archived"] = len(expired)

    # 3. Hard-delete archived STM items once the grace period is over (no tombstone: this is
    #    routine expiry, not the user asking to forget, so the fact may be learned again).
    stale = [r["id"] for r in svc.db.all(
        "SELECT id FROM memories WHERE tier = 'stm' AND status = 'archived' AND updated_at < ?",
        (t - svc.cfg.stm_grace_h * 3600,),
    )]  # fmt: skip
    if stale:
        svc._purge_quietly(stale)
    stats["purged"] = len(stale)

    # 4. Scratch pools whose subagent never closed them.
    for p in svc.db.all("SELECT id FROM pools WHERE kind = 'scratch' AND closed_at IS NULL AND expires_at < ?", (t,)):
        svc.pool_close("sweeper", p["id"], reason="expired")
        stats["pools_closed"] += 1

    # 5. Sessions that never produced a memory. Ended ones go now; open ones with no messages at all go after a
    #    day (Claude Code doesn't always send SessionEnd, e.g. when the terminal is closed).
    stats["empty_sessions_dropped"] = 0
    for r in svc.db.all(
        "SELECT id, ended_at FROM sessions s WHERE ended_at IS NOT NULL"
        " OR (started_at < ? AND NOT EXISTS (SELECT 1 FROM events e WHERE e.session_id = s.id))",
        (t - svc.cfg.stm_grace_h * 3600,),
    ):
        stats["empty_sessions_dropped"] += svc.drop_if_empty(r["id"], abandoned=r["ended_at"] is None)

    # 6. Raw events older than the STM window that no memory cites any more.
    cutoff = t - (svc.cfg.stm_max_ttl_h + svc.cfg.stm_grace_h) * 3600
    stats["events_dropped"] = svc.db.execute(
        "DELETE FROM events WHERE created_at < ? AND id NOT IN (SELECT event_id FROM memory_sources)", (cutoff,)
    ).rowcount
    if stats["purged"] or stats["events_dropped"]:
        svc.db.compact()
    svc.db.set_meta("last_sweep", str(t))
    return stats


MIN_SAMPLES = 20


def suggest_ttl(svc: "MemoryService") -> dict:
    """Pick BASE_TTL from how old STM items are when they're actually reused (95th percentile)."""
    ages: list[float] = []
    for r in svc.db.all("SELECT meta FROM metrics WHERE op = 'recall'"):
        ages.extend(json.loads(r["meta"]).get("stm_ages_h", []))
    if len(ages) < MIN_SAMPLES:
        return {"samples": len(ages), "suggested_h": None, "current_h": svc.cfg.stm_base_ttl_h,
                "note": f"need at least {MIN_SAMPLES} reuses of short-term items before suggesting a lifetime"}  # fmt: skip
    ages.sort()

    def pct(q: float) -> float:
        return round(ages[min(len(ages) - 1, int(q * len(ages)))], 1)

    p95 = pct(0.95)
    return {
        "samples": len(ages),
        "p50_h": pct(0.5),
        "p95_h": p95,
        "current_h": svc.cfg.stm_base_ttl_h,
        # Clamped to 2-4 days: shorter loses context between sessions, longer is what LTM is for.
        "suggested_h": max(48.0, min(96.0, p95)),
    }
