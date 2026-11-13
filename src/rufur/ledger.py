"""Low-level writes to the authoritative ledger and its derived views.

Nothing here makes decisions; the write/read/forget packages decide and call
these primitives. Every derived row (FTS, vector, edge, chunk, typed row) is
recorded in `lineage` at the moment it's created.
"""

from __future__ import annotations

import itertools
import json
import os
from typing import Any

from .db.conn import Database, dumps, pack_vector
from .events_bus import EventBus
from .textutil import new_id, now, sha256

MEMORY_FIELDS = (
    "id", "pool_id", "session_id", "type", "subject", "predicate", "value", "text", "scope",
    "valid_from", "valid_to", "status", "trust", "confidence", "importance", "sensitivity",
    "owner", "author_agent", "source_type", "superseded_by", "flags", "pinned",
    "created_at", "updated_at", "tier", "store", "target_pool", "expires_at", "volatility",
    "revalidate_at", "corroboration", "status_reason",
)  # fmt: skip


class Ledger:
    def __init__(self, db: Database, bus: EventBus):
        self.db = db
        self.bus = bus
        self._hlc_counter = itertools.count()
        self.node = db.meta("node_id") or new_id("node")
        db.set_meta("node_id", self.node)

    # -- clock and logs ----------------------------------------------------

    def hlc(self) -> str:
        # Wall clock (ns) + per-process counter + node id. Enough ordering for
        # the local op log; the cloud sync layer will replace this with a real HLC.
        return f"{int(now() * 1e9):020d}-{next(self._hlc_counter):06d}-{self.node}"

    def op(self, op: str, target: str, agent: str | None, payload: dict | None = None) -> None:
        self.db.execute(
            "INSERT INTO ops(hlc, op, target, payload, agent, created_at) VALUES(?,?,?,?,?,?)",
            (self.hlc(), op, target, dumps(payload or {}), agent, now()),
        )

    def audit(self, agent: str | None, action: str, target: str | None, detail: dict | None = None) -> None:
        self.db.execute(
            "INSERT INTO audit(agent, action, target, detail, created_at) VALUES(?,?,?,?,?)",
            (agent, action, target, dumps(detail or {}), now()),
        )

    def metric(self, op: str, latency_ms: float, *, path: str | None = None, agent: str | None = None,
               tokens_in: int = 0, tokens_out: int = 0, llm_tokens: int = 0, meta: dict | None = None) -> None:  # fmt: skip
        self.db.execute(
            "INSERT INTO metrics(op, path, agent, latency_ms, tokens_in, tokens_out, llm_tokens, meta, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (op, path, agent, latency_ms, tokens_in, tokens_out, llm_tokens, dumps(meta or {}), now()),
        )

    # -- events ------------------------------------------------------------

    def add_event(self, *, pool_id: str, agent: str, role: str, source_type: str, content: str,
                  session_id: str | None = None) -> dict[str, Any]:  # fmt: skip
        ev = {
            "id": new_id("ev"),
            "session_id": session_id,
            "pool_id": pool_id,
            "agent": agent,
            "role": role,
            "source_type": source_type,
            "content": content,
            "content_hash": sha256(content),
            "created_at": now(),
        }
        self.db.execute(
            "INSERT INTO events(id, session_id, pool_id, agent, role, source_type, content, content_hash, created_at)"
            " VALUES(:id,:session_id,:pool_id,:agent,:role,:source_type,:content,:content_hash,:created_at)",
            ev,
        )
        self.bus.publish("event.created", id=ev["id"], session_id=session_id, role=role)
        return ev

    # -- memories ----------------------------------------------------------

    def insert_memory(self, mem: dict[str, Any]) -> dict[str, Any]:
        t = now()
        row = {f: None for f in MEMORY_FIELDS}
        row.update({"created_at": t, "updated_at": t, "valid_from": t, "flags": "[]", "pinned": 0, "confidence": 0.7,
                    "tier": "ltm", "store": "fact", "volatility": "slow", "corroboration": 1})  # fmt: skip
        row.update(mem)
        row.setdefault("id", None)
        row["id"] = row["id"] or new_id("mem")
        if isinstance(row["flags"], list):
            row["flags"] = dumps(row["flags"])
        cols = ", ".join(MEMORY_FIELDS)
        vals = ", ".join(f":{f}" for f in MEMORY_FIELDS)
        with self.db.tx():
            self.db.execute(f"INSERT INTO memories({cols}) VALUES({vals})", row)
            cur = self.db.execute(
                "INSERT INTO memories_fts(text, subject, predicate, value, memory_id) VALUES(?,?,?,?,?)",
                (row["text"], row["subject"], row["predicate"], row["value"], row["id"]),
            )
            self.add_lineage("fts", str(cur.lastrowid), row["id"])
            # Session and tier let the dashboard replay a session's history after its memories are deleted.
            self.op("create", row["id"], row["author_agent"], {"type": row["type"], "pool": row["pool_id"],
                                                               "session": row["session_id"], "tier": row["tier"]})  # fmt: skip
        self.bus.publish("memory.created", id=row["id"], status=row["status"], pool_id=row["pool_id"], type=row["type"])
        return row

    def update_memory(self, memory_id: str, **fields: Any) -> None:
        if not fields:
            return
        if isinstance(fields.get("flags"), list):
            fields["flags"] = dumps(fields["flags"])
        fields["updated_at"] = now()
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        self.db.execute(f"UPDATE memories SET {sets} WHERE id = :id", {**fields, "id": memory_id})
        if "status" in fields:
            self.bus.publish("memory.updated", id=memory_id, status=fields["status"])

    def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        m = self.db.one("SELECT * FROM memories WHERE id = ?", (memory_id,))
        if m:
            m["flags"] = json.loads(m["flags"])
        return m

    def add_source(self, memory_id: str, event_id: str, quote: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO memory_sources(memory_id, event_id, quote) VALUES(?,?,?)", (memory_id, event_id, quote)
        )

    def add_lineage(self, kind: str, child_id: str, memory_id: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO lineage(child_kind, child_id, memory_id) VALUES(?,?,?)", (kind, child_id, memory_id)
        )

    # -- derived views -----------------------------------------------------

    def index_vector(self, kind: str, item_id: str, memory_id: str, vec: list[float]) -> None:
        with self.db.tx():
            cur = self.db.execute("INSERT INTO vec_map(kind, item_id, memory_id) VALUES(?,?,?)", (kind, item_id, memory_id))
            rowid = cur.lastrowid
            self.db.execute("INSERT INTO vec_items(rowid, embedding) VALUES(?, ?)", (rowid, pack_vector(vec)))
            self.add_lineage("vector", str(rowid), memory_id)

    def add_edge(self, src: str, relation: str, dst: str, memory_id: str, pool_id: str) -> str:
        for name in (src, dst):
            self.db.execute("INSERT OR IGNORE INTO entities(id, name, created_at) VALUES(?,?,?)", (new_id("ent"), name, now()))
        eid = new_id("edge")
        self.db.execute(
            "INSERT INTO edges(id, src, relation, dst, memory_id, pool_id, created_at) VALUES(?,?,?,?,?,?,?)",
            (eid, src, relation, dst, memory_id, pool_id, now()),
        )
        self.add_lineage("edge", eid, memory_id)
        return eid

    def add_typed_row(self, table_name: str, row: dict, memory_id: str, pool_id: str) -> str:
        rid = new_id("row")
        self.db.execute(
            "INSERT INTO typed_rows(id, table_name, row, memory_id, pool_id) VALUES(?,?,?,?,?)",
            (rid, table_name, dumps(row), memory_id, pool_id),
        )
        self.add_lineage("typed_row", rid, memory_id)
        return rid

    def add_file(self, path: str, content: str, memory_id: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        self.add_lineage("file", path, memory_id)

    def add_chunk(self, memory_id: str, path: str, index: int, text: str) -> str:
        cid = new_id("chunk")
        self.db.execute(
            "INSERT INTO file_chunks(id, memory_id, path, chunk_index, text) VALUES(?,?,?,?,?)",
            (cid, memory_id, path, index, text),
        )
        self.db.execute("INSERT INTO chunks_fts(text, chunk_id, memory_id) VALUES(?,?,?)", (text, cid, memory_id))
        self.add_lineage("file_chunk", cid, memory_id)
        return cid
