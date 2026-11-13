"""SQLite connection with FTS5 + sqlite-vec, guarded by one re-entrant lock.

The daemon is the single writer. All access goes through `Database`, which
serialises statements with an RLock so REST handlers, MCP tools and the
background extraction worker can share one connection safely.
"""

from __future__ import annotations

import json
import sqlite3
import struct
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import sqlite_vec

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()

# Columns added after the first schema. Applied with ALTER TABLE when missing, so databases
# created by older versions keep working.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("agents", "user_id", "TEXT NOT NULL DEFAULT 'me'"),
    ("agents", "last_seen", "REAL"),
    ("pools", "parent_pool", "TEXT"),
    ("pools", "owner_agent", "TEXT"),
    ("pools", "owner_session", "TEXT"),
    ("pools", "expires_at", "REAL"),
    ("pools", "purpose", "TEXT"),
    ("pools", "cap_hash", "TEXT"),  # sha256 of the secret part of the pool's memory address
    ("memories", "tier", "TEXT NOT NULL DEFAULT 'ltm'"),  # stm | ltm
    ("memories", "store", "TEXT NOT NULL DEFAULT 'fact'"),  # profile | fact | table | graph | procedure | journal | discard
    ("memories", "target_pool", "TEXT"),  # long-term pool an STM item is promoted into
    ("memories", "expires_at", "REAL"),
    ("memories", "volatility", "TEXT NOT NULL DEFAULT 'slow'"),  # static | slow | volatile
    ("memories", "revalidate_at", "REAL"),
    ("memories", "corroboration", "INTEGER NOT NULL DEFAULT 1"),
    ("memories", "consolidation_run", "TEXT"),
    ("conflicts", "ask_user", "INTEGER NOT NULL DEFAULT 0"),  # only the user can settle it; never defaulted
]

POST_MIGRATION_INDEXES = (
    "CREATE INDEX IF NOT EXISTS memories_tier ON memories(tier, consolidation_run)",
    "CREATE INDEX IF NOT EXISTS memories_expiry ON memories(tier, expires_at)",
)


def pack_vector(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


class Database:
    def __init__(self, path: Path | str, embed_dim: int):
        self.path = Path(path)
        self.embed_dim = embed_dim
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        # Overwrite freed pages with zeros so deleted content doesn't linger on disk.
        self.conn.execute("PRAGMA secure_delete=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        with self.lock:
            self.conn.executescript(SCHEMA)
            for table, column, ddl in MIGRATIONS:
                cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            for sql in POST_MIGRATION_INDEXES:
                self.conn.execute(sql)
            stored = self.meta("embed_dim")
            if stored is not None and int(stored) != self.embed_dim:
                raise RuntimeError(
                    f"database was built with embed_dim={stored}, config says {self.embed_dim}; "
                    "run `rufur reindex` or point RUFUR_HOME at a fresh directory"
                )
            self.conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_items USING vec0(embedding float[{self.embed_dim}] distance_metric=cosine)"
            )
            if stored is None:
                self.set_meta("embed_dim", str(self.embed_dim))
            # FTS5 normally leaves deleted terms in old index segments until a merge; secure-delete removes
            # them at delete time, so a forget needn't rebuild the index (persistent per-table setting).
            for fts in ("memories_fts", "chunks_fts"):
                self.conn.execute(f"INSERT INTO {fts}({fts}, rank) VALUES('secure-delete', 1)")

    # -- helpers -----------------------------------------------------------

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        with self.lock:
            self.conn.executemany(sql, rows)

    def all(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def scalar(self, sql: str, params: tuple | dict = ()) -> Any:
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
            return row[0] if row else None

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def meta(self, key: str) -> str | None:
        return self.scalar("SELECT value FROM meta WHERE key = ?", (key,))

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def compact(self) -> None:
        """Merge FTS segments, rebuild indexes, VACUUM and truncate the WAL.

        Run after a hard delete so purged bytes leave the file entirely.
        """
        with self.lock:
            self.conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('optimize')")
            self.conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
            self.conn.execute("VACUUM")
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def scrub(self) -> None:
        """Push a delete's zeroed pages out of the WAL and truncate it. With secure_delete and FTS5
        secure-delete on, this is all a forget needs: cost scales with what was deleted, not the file."""
        with self.lock:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self) -> None:
        with self.lock:
            self.conn.close()


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
