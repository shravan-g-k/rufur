"""Read models for the dashboard: sessions and their subagent pools, per-pool memories and history,
token usage over time, and what forgetting did.

Everything here is read-only and content-safe in the same way as the rest of the UI API: memories above
the caller's clearance come back with their text hidden, and deletions are described by ids, counts and
receipts, never by the forgotten text.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import TYPE_CHECKING

from .forget import fade
from .forget import receipts as receipts_mod
from .forget.engine import REDACTED
from .textutil import estimate_tokens, now
from .write.trust import SENSITIVITY_ORDER

if TYPE_CHECKING:
    from .service import MemoryService

UNATTACHED = "unattached"
MEMORY_COLS = ("id, text, type, tier, status, store, trust, sensitivity, created_at, updated_at, expires_at, "
               "access_count, last_accessed, pool_id, session_id, author_agent, flags, status_reason")  # fmt: skip


def _user_agents(svc: "MemoryService", agent: str) -> list[str]:
    """Root agents of the caller's user. Subagent identities belong to `scratch:<pool>` users."""
    user = svc.agent(agent)["user_id"]
    return [r["name"] for r in svc.db.all("SELECT name FROM agents WHERE user_id = ?", (user,))]


def _marks(xs) -> str:
    return ",".join("?" * len(xs))


def _visible(svc: "MemoryService", agent: str, rows: list[dict]) -> list[dict]:
    clearance = SENSITIVITY_ORDER.index(svc.agent(agent)["clearance"])
    for r in rows:
        r["flags"] = json.loads(r["flags"]) if isinstance(r.get("flags"), str) else r.get("flags", [])
        if SENSITIVITY_ORDER.index(r["sensitivity"]) > clearance:
            r["text"] = f"[{r['sensitivity']} memory, text hidden]"
    return rows


def _close_detail(svc: "MemoryService", pool_id: str) -> dict:
    row = svc.db.one("SELECT detail FROM audit WHERE action = 'pool.close' AND target = ? ORDER BY id DESC LIMIT 1", (pool_id,))
    return json.loads(row["detail"]) if row else {}


def _pool_summary(svc: "MemoryService", p: dict) -> dict:
    seeded = {r["tier"]: r["n"] for r in svc.db.all(
        "SELECT m.tier, count(*) AS n FROM pool_members pm JOIN memories m ON m.id = pm.memory_id "
        "WHERE pm.pool_id = ? GROUP BY m.tier", (p["id"],))}  # fmt: skip
    detail = _close_detail(svc, p["id"]) if p["closed_at"] else {}
    created = svc.db.scalar("SELECT count(*) FROM ops WHERE op = 'create' AND json_extract(payload, '$.pool') = ?", (p["id"],))
    expired = bool(not p["closed_at"] and p["expires_at"] and p["expires_at"] < now())
    return {
        "id": p["id"], "purpose": p["purpose"], "owner_agent": p["owner_agent"], "session_id": p["owner_session"],
        "created_at": p["created_at"], "expires_at": p["expires_at"], "closed_at": p["closed_at"],
        "status": "closed" if p["closed_at"] else "expired" if expired else "open",
        "seeded_stm": seeded.get("stm", 0), "seeded_ltm": seeded.get("ltm", 0),
        "own": svc.db.scalar("SELECT count(*) FROM memories WHERE pool_id = ?", (p["id"],)),
        "own_written": created, "merged": detail.get("merged"), "discarded": detail.get("discarded"),
        "close_reason": detail.get("reason"),
    }  # fmt: skip


def sessions(svc: "MemoryService", agent: str, limit: int = 100) -> dict:
    agents = _user_agents(svc, agent)
    rows = svc.db.all(
        f"SELECT * FROM sessions WHERE agent IN ({_marks(agents)}) ORDER BY started_at DESC LIMIT ?", (*agents, limit)
    ) if agents else []  # fmt: skip
    pools = svc.db.all(
        f"SELECT * FROM pools WHERE kind = 'scratch' AND owner_agent IN ({_marks(agents)}) ORDER BY created_at",
        tuple(agents),
    ) if agents else []  # fmt: skip
    by_session: dict[str, list[dict]] = defaultdict(list)
    ids = {s["id"] for s in rows}
    for p in pools:
        by_session[p["owner_session"] if p["owner_session"] in ids else UNATTACHED].append(_pool_summary(svc, p))

    out = []
    for s in rows:
        tiers: dict[str, dict[str, int]] = {"stm": {}, "ltm": {}}
        for r in svc.db.all("SELECT tier, status, count(*) AS n FROM memories WHERE session_id = ? GROUP BY tier, status", (s["id"],)):
            tiers.setdefault(r["tier"], {})[r["status"]] = r["n"]
        ev = svc.db.all("SELECT content, redacted FROM events WHERE session_id = ?", (s["id"],))
        out.append({
            **s, "events": len(ev), "redacted_events": sum(e["redacted"] for e in ev),
            "event_tokens": sum(estimate_tokens(e["content"]) for e in ev),
            "stm": tiers["stm"], "ltm": tiers["ltm"], "forgotten": _forgotten_count(svc, s["id"]),
            "composition": _composition(svc, s["id"]),
            "pools": by_session.get(s["id"], []),
        })  # fmt: skip
    c = svc.cfg
    return {
        "sessions": out, "unattached_pools": by_session.get(UNATTACHED, []), "totals": _totals(svc),
        "ttl": {"base_h": c.stm_base_ttl_h, "extend_h": c.stm_extend_h, "max_h": c.stm_max_ttl_h, "grace_h": c.stm_grace_h,
                "scratch_min": c.scratch_ttl_min},
        "now": now(),
    }  # fmt: skip


def _tier_counts(svc: "MemoryService", where: str = "1") -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {"stm": {}, "ltm": {}}
    for r in svc.db.all(f"SELECT m.tier, m.status, count(*) AS n FROM memories m JOIN pools p ON p.id = m.pool_id "
                        f"WHERE {where} GROUP BY m.tier, m.status"):  # fmt: skip
        out.setdefault(r["tier"], {})[r["status"]] = r["n"]
    return out


def _totals(svc: "MemoryService") -> dict:
    """Counts over every memory and pool, whether or not it belongs to a session. Explicit remembers,
    merged subagent results and journals have no session, so per-session sums miss them."""
    pools: dict[str, dict[str, int]] = {}
    for r in svc.db.all("SELECT kind, closed_at IS NULL AS open, count(*) AS n FROM pools GROUP BY kind, open"):
        pools.setdefault(r["kind"], {"open": 0, "closed": 0})["open" if r["open"] else "closed"] = r["n"]
    return {
        "memories": _tier_counts(svc, "p.kind != 'scratch'"),
        "unsessioned": _tier_counts(svc, "m.session_id IS NULL AND p.kind != 'scratch'"),
        "scratch": _tier_counts(svc, "p.kind = 'scratch'"),
        "pools": pools,
        "storage": _storage(svc),
    }  # fmt: skip


def _created_ids(svc: "MemoryService", key: str, value: str) -> list[str]:
    """Every memory ever created in a session or pool, including ones since deleted (ops outlive them)."""
    ids = [r["target"] for r in svc.db.all(
        f"SELECT target FROM ops WHERE op = 'create' AND json_extract(payload, '$.{key}') = ?", (value,))]  # fmt: skip
    col = "session_id" if key == "session" else "pool_id"
    ids += [r["id"] for r in svc.db.all(f"SELECT id FROM memories WHERE {col} = ?", (value,))]
    return list(dict.fromkeys(ids))


def _composition(svc: "MemoryService", session_id: str) -> dict[str, int]:
    """Where every memory a session ever created sits now, each counted exactly once.

    Live rows are bucketed by tier and status; rows that no longer exist are bucketed by the delete op the
    ledger kept (forgotten on request, or purged after expiry/merge). Nothing is estimated.
    """
    out = dict.fromkeys(("ltm", "stm", "disputed", "held", "fading", "superseded", "expired", "rejected", "forgotten"), 0)
    ids = _created_ids(svc, "session", session_id)
    if not ids:
        return out
    marks = _marks(ids)
    live = svc.db.all(f"SELECT id, tier, status, flags FROM memories WHERE id IN ({marks})", tuple(ids))
    bucket = {"disputed": "disputed", "quarantined": "held", "superseded": "superseded", "archived": "expired",
              "rejected": "rejected"}  # fmt: skip
    for r in live:
        if r["status"] == "archived" and f'"{fade.FADING}"' in r["flags"]:
            out["fading"] += 1
        else:
            out[r["tier"] if r["status"] == "active" else bucket.get(r["status"], "expired")] += 1
    gone = [i for i in ids if i not in {r["id"] for r in live}]
    if gone:
        for r in svc.db.all(
            f"SELECT target, max(json_extract(payload, '$.reason') IS NULL) AS on_request FROM ops "
            f"WHERE op = 'delete' AND target IN ({_marks(gone)}) GROUP BY target", tuple(gone)):  # fmt: skip
            out["forgotten" if r["on_request"] else "expired"] += 1
    return out


def _storage(svc: "MemoryService") -> dict:
    """How live long-term knowledge is physically kept: store per memory, plus the indexes and side tables."""
    stores = {r["store"]: r["n"] for r in svc.db.all(
        "SELECT store, count(*) AS n FROM memories WHERE tier = 'ltm' AND status IN ('active', 'disputed') GROUP BY store")}  # fmt: skip
    return {
        "stores": stores,
        "edges": svc.db.scalar("SELECT count(*) FROM edges") or 0,
        "entities": svc.db.scalar("SELECT count(*) FROM entities") or 0,
        "typed_rows": svc.db.scalar("SELECT count(*) FROM typed_rows") or 0,
        "tables": svc.db.scalar("SELECT count(*) FROM table_schemas") or 0,
        "vectors": svc.db.scalar("SELECT count(*) FROM vec_map") or 0,
        "events": svc.db.scalar("SELECT count(*) FROM events") or 0,
        "ops": svc.db.scalar("SELECT count(*) FROM ops") or 0,
    }


def _forgotten_count(svc: "MemoryService", session_id: str) -> int:
    ids = _created_ids(svc, "session", session_id)
    if not ids:
        return 0
    return svc.db.scalar(
        f"SELECT count(DISTINCT target) FROM ops WHERE op = 'delete' AND target IN ({_marks(ids)}) "
        "AND json_extract(payload, '$.reason') IS NULL", tuple(ids))  # fmt: skip


def _scope(svc: "MemoryService", agent: str, kind: str, ident: str) -> dict:
    if kind == "unsessioned":
        return {}
    agents = _user_agents(svc, agent)
    if kind == "session":
        s = svc.db.one("SELECT * FROM sessions WHERE id = ?", (ident,))
        if s is None or s["agent"] not in agents:
            raise LookupError(f"session {ident!r} not found")
        return s
    p = svc.db.one("SELECT * FROM pools WHERE id = ? AND kind = 'scratch'", (ident,))
    if p is None or p["owner_agent"] not in agents:
        raise LookupError(f"pool {ident!r} not found")
    return p


def memories(svc: "MemoryService", agent: str, kind: str, ident: str) -> dict:
    _scope(svc, agent, kind, ident)
    if kind == "session":
        rows = svc.db.all(f"SELECT {MEMORY_COLS}, 'session' AS origin FROM memories WHERE session_id = ? "
                          "ORDER BY created_at DESC", (ident,))  # fmt: skip
    elif kind == "unsessioned":
        rows = svc.db.all(
            f"SELECT {', '.join('m.' + c.strip() for c in MEMORY_COLS.split(','))}, 'session' AS origin FROM memories m "
            "JOIN pools p ON p.id = m.pool_id WHERE m.session_id IS NULL AND p.kind != 'scratch' ORDER BY m.created_at DESC"
        )  # fmt: skip
    else:
        rows = svc.db.all(
            f"SELECT {', '.join('m.' + c.strip() for c in MEMORY_COLS.split(','))}, 'seeded' AS origin FROM pool_members pm "
            "JOIN memories m ON m.id = pm.memory_id WHERE pm.pool_id = ? "
            f"UNION ALL SELECT {MEMORY_COLS}, 'own' FROM memories WHERE pool_id = ? ORDER BY created_at DESC",
            (ident, ident),
        )  # fmt: skip
    return {"items": _visible(svc, agent, rows), "now": now()}


def timeline(svc: "MemoryService", agent: str, kind: str, ident: str) -> dict:
    scope = _scope(svc, agent, kind, ident)
    ids = _created_ids(svc, "session" if kind == "session" else "pool", ident)
    ops = svc.db.all(
        f"SELECT op, target, payload, agent, created_at FROM ops WHERE target IN ({_marks(ids)}) ORDER BY seq", tuple(ids)
    ) if ids else []  # fmt: skip
    texts = {r["id"]: r["text"] for r in _visible(svc, agent, svc.db.all(
        f"SELECT id, text, sensitivity, flags FROM memories WHERE id IN ({_marks(ids)})", tuple(ids)))} if ids else {}  # fmt: skip

    events: list[dict] = []
    for a in svc.db.all("SELECT agent, action, target, detail, created_at FROM audit WHERE target = ? ORDER BY id", (ident,)):
        events.append({"t": a["created_at"], "kind": a["action"], "agent": a["agent"], "detail": json.loads(a["detail"])})
    if kind == "scratch":
        seeded = svc.db.scalar("SELECT count(*) FROM pool_members WHERE pool_id = ?", (ident,))
        if not any(e["kind"] == "pool.spawn" for e in events):
            events.append({"t": scope["created_at"], "kind": "pool.spawn", "agent": scope["owner_agent"],
                           "detail": {"seeded": seeded}})  # fmt: skip

    # Replay the ops to count where each memory sits over time: STM, LTM, replaced, forgotten or expired.
    tier: dict[str, str] = {}
    state = {"stm": 0, "ltm": 0, "superseded": 0, "forgotten": 0, "expired": 0}
    series = [{"t": scope.get("started_at") or scope["created_at"], **state}]
    for o in ops:
        p = json.loads(o["payload"])
        mid, op = o["target"], o["op"]
        before = dict(state)
        if op == "create":
            tier[mid] = p.get("tier") or ("stm" if kind == "session" else "own")
            bucket = "ltm" if tier[mid] == "ltm" else "stm"
            state[bucket] += 1
            tier[mid] = bucket
        elif op == "promote" and tier.get(mid) == "stm":
            state["stm"] -= 1
            state["ltm"] += 1
            tier[mid] = "ltm"
        elif op == "supersede" and tier.get(mid) in ("stm", "ltm"):
            state[tier[mid]] -= 1
            state["superseded"] += 1
            tier[mid] = "superseded"
        elif op in ("archive", "delete") and mid in tier and tier[mid] not in ("forgotten", "expired"):
            if tier[mid] in ("stm", "ltm", "superseded"):
                state[tier[mid]] -= 1
            # A user forget carries no reason; expiry, merges and pool close-out do.
            gone = "forgotten" if op == "delete" and not p.get("reason") else "expired"
            state[gone] += 1
            tier[mid] = gone
        events.append({"t": o["created_at"], "kind": f"memory.{op}", "agent": o["agent"], "memory_id": mid,
                       "text": texts.get(mid), "detail": p})  # fmt: skip
        if state != before:
            series.append({"t": o["created_at"], **state})
    events.sort(key=lambda e: e["t"])
    if series[-1]["t"] < now():
        series.append({**series[-1], "t": now()})
    return {"events": events, "series": series}


ACTIVITY_GAP_S = 20 * 60  # a quiet spell this long starts a new activity session on the token timeline


def _empty_bucket() -> dict:
    return {"recall": 0, "remember": 0, "extract": 0, "forget": 0, "llm": 0, "served": 0}


def tokens(svc: "MemoryService", bucket: str = "session", since: float | None = None) -> dict:
    """Token use over time. `session` groups operations into bursts of activity (a new one after 20 quiet
    minutes, whichever agent is working); `hour` is a continuous hourly line over the last two days."""
    since = since if since is not None else now() - (7 * 86400 if bucket == "session" else 2 * 86400)
    rows = svc.db.all(
        "SELECT op, agent, tokens_in, tokens_out, llm_tokens, created_at FROM metrics WHERE created_at >= ? ORDER BY created_at",
        (since,),
    )
    buckets: dict[float, dict] = {}
    if bucket == "hour":  # every hour gets a point, so a quiet hour reads as zero rather than a line across it
        h = (since // 3600) * 3600
        while h <= now():
            buckets[h] = _empty_bucket()
            h += 3600
    by_agent: dict[str, dict] = defaultdict(lambda: {"calls": 0, "tokens_in": 0, "tokens_out": 0, "llm_tokens": 0})
    start = last = None
    for r in rows:
        if bucket == "session":
            if last is None or r["created_at"] - last > ACTIVITY_GAP_S:
                start = r["created_at"]
                buckets[start] = _empty_bucket() | {"end": start, "calls": 0, "agents": []}
            last = r["created_at"]
            b = buckets[start]
            b["end"] = last
            b["calls"] += 1
            if (name := r["agent"] or "system") not in b["agents"]:
                b["agents"].append(name)
        else:
            b = buckets.setdefault((r["created_at"] // 3600) * 3600, _empty_bucket())
        b[r["op"]] = b.get(r["op"], 0) + r["tokens_in"] + r["tokens_out"] + r["llm_tokens"]
        b["llm"] += r["llm_tokens"]
        b["served"] += r["tokens_out"]
        a = by_agent[r["agent"] or "system"]
        a["calls"] += 1
        a["tokens_in"] += r["tokens_in"]
        a["tokens_out"] += r["tokens_out"]
        a["llm_tokens"] += r["llm_tokens"]

    # What sending the whole conversation history with every recall would have cost, against what was sent.
    events = svc.db.all("SELECT content, created_at FROM events ORDER BY created_at")
    recalls = svc.db.all("SELECT tokens_out, created_at FROM metrics WHERE op = 'recall' ORDER BY id")
    history, sent, cumulative, i = 0, 0, [], 0
    baseline = 0
    for r in recalls:
        while i < len(events) and events[i]["created_at"] <= r["created_at"]:
            history += estimate_tokens(events[i]["content"])
            i += 1
        baseline += history
        sent += r["tokens_out"]
        if r["created_at"] >= since:
            cumulative.append({"t": r["created_at"], "full_history": baseline, "rufur": sent})
    return {
        "bucket": bucket, "series": [{"t": t, **v} for t, v in sorted(buckets.items())],
        "cumulative": cumulative, "by_agent": [{"agent": k, **v} for k, v in sorted(by_agent.items())],
        "totals": {k: sum(r[k] for r in rows) for k in ("tokens_in", "tokens_out", "llm_tokens")},
    }  # fmt: skip


def forgetting(svc: "MemoryService", agent: str) -> dict:
    agents = _user_agents(svc, agent)
    redacted = svc.db.all(
        f"SELECT id, session_id, agent, role, content, created_at FROM events WHERE redacted = 1 "
        f"AND agent IN ({_marks(agents)}) ORDER BY created_at DESC LIMIT 200", tuple(agents)
    ) if agents else []  # fmt: skip
    expiry = {r["k"]: r["n"] for r in svc.db.all(
        "SELECT CASE WHEN op = 'archive' AND json_extract(payload, '$.reason') = 'unused' THEN 'faded' "
        "WHEN op = 'archive' THEN 'archived' WHEN json_extract(payload, '$.reason') IS NULL THEN 'forgotten' "
        "ELSE 'purged' END AS k, count(*) AS n FROM ops WHERE op IN ('archive', 'delete') GROUP BY k")}  # fmt: skip
    return {
        "receipts": receipts_mod.list_receipts(svc.db, 100), "chain": receipts_mod.verify_chain(svc.cfg, svc.db),
        "redacted_events": redacted, "placeholder": REDACTED, "counts": expiry,
        "tombstones": svc.db.scalar("SELECT count(*) FROM tombstones"),
        "fading": fade.fading(svc), "fade": {"after_days": svc.cfg.fade_after_days, "below": svc.cfg.fade_below,
                                             "purge_days": svc.cfg.fade_purge_days},
    }  # fmt: skip
