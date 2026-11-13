"""Raw, read-only view of everything RUFUR stores: every table in the SQLite file (core tables, the FTS5
index and its shadow tables, the sqlite-vec vector store and its shadow tables, the graph), plus the
derived files on disk. Rows come back exactly as stored; nothing is joined, filtered by ACL or rewritten.

It opens its own connection with `mode=ro` and `query_only`, so nothing here can modify the database.
The schema declares no foreign keys, so `REFS` spells out which id columns point where; those links
are the only thing this module adds, and they describe the schema, not the data.
"""

from __future__ import annotations

import base64
import os
import sqlite3
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator

import sqlite_vec

if TYPE_CHECKING:
    from .service import MemoryService

# (table, column) -> (table, column). A dict target picks the table from another column of the same row.
REFS: dict[tuple[str, str], Any] = {
    ("memories", "pool_id"): ("pools", "id"),
    ("memories", "session_id"): ("sessions", "id"),
    ("memories", "superseded_by"): ("memories", "id"),
    ("memories", "target_pool"): ("pools", "id"),
    ("memories", "consolidation_run"): ("consolidation_runs", "id"),
    ("memories", "author_agent"): ("agents", "name"),
    ("memory_sources", "memory_id"): ("memories", "id"),
    ("memory_sources", "event_id"): ("events", "id"),
    ("lineage", "memory_id"): ("memories", "id"),
    ("lineage", "child_id"): {"by": "child_kind", "fts": ("memories_fts", "rowid"), "vector": ("vec_items", "rowid"),
                              "edge": ("edges", "id"), "typed_row": ("typed_rows", "id"),
                              "file_chunk": ("file_chunks", "id"), "journal": ("memories", "id")},  # fmt: skip
    ("events", "session_id"): ("sessions", "id"),
    ("events", "pool_id"): ("pools", "id"),
    ("events", "agent"): ("agents", "name"),
    ("sessions", "agent"): ("agents", "name"),
    ("sessions", "pool_id"): ("pools", "id"),
    ("sessions", "target_pool"): ("pools", "id"),
    ("sessions", "task_id"): ("tasks", "id"),
    ("pools", "parent_pool"): ("pools", "id"),
    ("pools", "owner_session"): ("sessions", "id"),
    ("pools", "owner_agent"): ("agents", "name"),
    ("pool_members", "pool_id"): ("pools", "id"),
    ("pool_members", "memory_id"): ("memories", "id"),
    ("edges", "memory_id"): ("memories", "id"),
    ("edges", "pool_id"): ("pools", "id"),
    ("edges", "src"): ("entities", "name"),
    ("edges", "dst"): ("entities", "name"),
    ("typed_rows", "memory_id"): ("memories", "id"),
    ("typed_rows", "pool_id"): ("pools", "id"),
    ("typed_rows", "table_name"): ("table_schemas", "table_name"),
    ("file_chunks", "memory_id"): ("memories", "id"),
    ("vec_map", "rowid"): ("vec_items", "rowid"),
    ("vec_map", "memory_id"): ("memories", "id"),
    ("vec_map", "item_id"): {"by": "kind", "memory": ("memories", "id"), "file_chunk": ("file_chunks", "id")},
    ("memories_fts", "memory_id"): ("memories", "id"),
    ("chunks_fts", "memory_id"): ("memories", "id"),
    ("chunks_fts", "chunk_id"): ("file_chunks", "id"),
    ("conflicts", "user_id"): ("agents", "user_id"),
    ("tasks", "from_agent"): ("agents", "name"),
    ("tasks", "to_agent"): ("agents", "name"),
    ("ops", "agent"): ("agents", "name"),
    ("audit", "agent"): ("agents", "name"),
    ("metrics", "agent"): ("agents", "name"),
}

# Which store each table belongs to, for grouping. Shadow tables follow their virtual table.
GROUPS = {
    "graph": ("entities", "edges"),
    "vector": ("vec_items", "vec_map"),
    "fts": ("memories_fts", "chunks_fts"),
}
BLOB_LIMIT = 64 * 1024


@contextmanager
def connect(svc: "MemoryService") -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(f"file:{svc.cfg.db_path}?mode=ro", uri=True, check_same_thread=False)
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _value(v: Any) -> Any:
    if isinstance(v, bytes):
        return {"$blob": base64.b64encode(v[:BLOB_LIMIT]).decode(), "bytes": len(v), "truncated": len(v) > BLOB_LIMIT}
    return v


def _objects(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name").fetchall()
    return [{"type": t, "name": n, "tbl_name": tn, "sql": s} for t, n, tn, s in rows]


def _group(name: str, virtual: set[str]) -> tuple[str, str | None]:
    """(group, shadow_of) for a table."""
    parent = next((v for v in virtual if name != v and name.startswith(v + "_")), None)
    base = parent or name
    for group, names in GROUPS.items():
        if base in names:
            return group, parent
    return ("system" if name.startswith("sqlite_") else "relational"), parent


def catalog(svc: "MemoryService") -> dict:
    with connect(svc) as conn:
        objects = _objects(conn)
        virtual = {o["name"] for o in objects if o["type"] == "table" and (o["sql"] or "").upper().startswith("CREATE VIRTUAL")}
        tables = []
        for o in objects:
            if o["type"] not in ("table", "view"):
                continue
            name = o["name"]
            cols = [{"name": c[1], "type": c[2], "notnull": bool(c[3]), "default": c[4], "pk": c[5], "hidden": c[6]}
                    for c in conn.execute(f"PRAGMA table_xinfo({_q(name)})")]  # fmt: skip
            try:
                count = conn.execute(f"SELECT count(*) FROM {_q(name)}").fetchone()[0]
            except sqlite3.Error as e:
                count, error = None, str(e)
            else:
                error = None
            group, shadow_of = _group(name, virtual)
            tables.append({
                "name": name, "type": "virtual" if name in virtual else o["type"], "sql": o["sql"], "rows": count,
                "error": error, "group": group, "shadow_of": shadow_of,
                "columns": [c for c in cols if not c["hidden"]],
                "has_rowid": _has_rowid(conn, name),
                "indexes": [{"name": i["name"], "sql": i["sql"]} for i in objects if i["type"] == "index" and i["tbl_name"] == name],
                "foreign_keys": [dict(zip(("id", "seq", "table", "from", "to"), fk[:5]))
                                 for fk in conn.execute(f"PRAGMA foreign_key_list({_q(name)})")],
            })  # fmt: skip
        sqlite_version = conn.execute("SELECT sqlite_version()").fetchone()[0]
        vec_version = conn.execute("SELECT vec_version()").fetchone()[0]

    names = {t["name"]: {c["name"] for c in t["columns"]} | ({"rowid"} if t["has_rowid"] else set()) for t in tables}
    links = []
    for (table, col), target in REFS.items():
        if col not in names.get(table, ()):
            continue
        targets = {k: v for k, v in target.items() if k != "by"} if isinstance(target, dict) else {"": target}
        for when, (tt, tc) in targets.items():
            if tc in names.get(tt, ()):
                links.append({"from_table": table, "from_column": col, "to_table": tt, "to_column": tc,
                              "when": {"column": target["by"], "equals": when} if when else None})  # fmt: skip

    db = svc.cfg.db_path
    files = []
    for root, _dirs, fnames in os.walk(svc.cfg.files_dir):
        for f in sorted(fnames):
            p = os.path.join(root, f)
            files.append({"path": os.path.relpath(p, svc.cfg.files_dir), "bytes": os.path.getsize(p), "modified": os.path.getmtime(p)})
    return {
        "database": {"path": str(db), "bytes": db.stat().st_size, "wal_bytes": _size(f"{db}-wal"), "sqlite": sqlite_version,
                     "sqlite_vec": vec_version, "embed_dim": svc.cfg.embed_dim},
        "tables": tables, "links": links, "files_dir": str(svc.cfg.files_dir), "files": files,
    }  # fmt: skip


def _size(path: str) -> int | None:
    return os.path.getsize(path) if os.path.exists(path) else None


def _has_rowid(conn: sqlite3.Connection, name: str) -> bool:
    try:
        conn.execute(f"SELECT rowid FROM {_q(name)} LIMIT 0")
        return True
    except sqlite3.Error:
        return False


def rows(svc: "MemoryService", table: str, *, limit: int = 100, offset: int = 0, column: str | None = None,
         value: str | None = None, desc: bool = False) -> dict:  # fmt: skip
    with connect(svc) as conn:
        if table not in {o["name"] for o in _objects(conn) if o["type"] in ("table", "view")}:
            raise LookupError(f"no table {table!r}")
        cols = [c[1] for c in conn.execute(f"PRAGMA table_xinfo({_q(table)})") if not c[6]]
        rowid = _has_rowid(conn, table)
        # Prepend the implicit rowid unless the table already has a column by that name (vec0, vec_map).
        select = (["rowid"] if rowid and "rowid" not in cols else []) + [_q(c) for c in cols]
        where, params = "", []
        if column is not None:
            if column not in cols and not (rowid and column == "rowid"):
                raise ValueError(f"no column {column!r} in {table}")
            where = f" WHERE {_q(column) if column != 'rowid' else 'rowid'} = ?"
            # Integer-affinity columns and vec0's rowid compare correctly against an int; TEXT columns
            # convert it back, so a digit string still matches a text id.
            params.append(int(value) if value is not None and value.lstrip("-").isdigit() else value)
        order = f" ORDER BY rowid {'DESC' if desc else 'ASC'}" if rowid else ""
        total = conn.execute(f"SELECT count(*) FROM {_q(table)}{where}", params).fetchone()[0]
        cur = conn.execute(f"SELECT {', '.join(select)} FROM {_q(table)}{where}{order} LIMIT ? OFFSET ?",
                           (*params, limit, offset))  # fmt: skip
        names = [d[0] for d in cur.description]
        out = [[_value(v) for v in r] for r in cur.fetchall()]
    return {"table": table, "columns": names, "rows": out, "total": total, "limit": limit, "offset": offset}
