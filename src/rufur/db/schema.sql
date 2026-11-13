-- RUFUR ledger. `events`, `memories`, `memory_sources` and `lineage` are the
-- authoritative record; FTS, vector, graph, file-chunk and typed-row tables are
-- derived views that can be rebuilt from them.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    key_hash    TEXT,
    clearance   TEXT NOT NULL DEFAULT 'confidential',  -- highest sensitivity it may read
    trust       TEXT NOT NULL DEFAULT 'medium_low',    -- trust of memories it authors
    quarantined INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pools (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    kind           TEXT NOT NULL DEFAULT 'long',       -- long | stm (per user) | scratch (per subagent)
    owner          TEXT,
    description    TEXT,
    retention_days INTEGER,
    acl            TEXT NOT NULL DEFAULT '{"read":["*"],"write":["*"]}',
    created_at     REAL NOT NULL,
    closed_at      REAL
);

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    agent       TEXT NOT NULL,
    pool_id     TEXT NOT NULL,   -- the temporary session pool
    target_pool TEXT NOT NULL,   -- where promoted memories go
    task_id     TEXT,
    started_at  REAL NOT NULL,
    ended_at    REAL
);

CREATE TABLE IF NOT EXISTS events (
    id           TEXT PRIMARY KEY,
    session_id   TEXT,
    pool_id      TEXT NOT NULL,
    agent        TEXT NOT NULL,
    role         TEXT NOT NULL,          -- user | assistant | tool | document | web
    source_type  TEXT NOT NULL,          -- user | document | agent | tool | web
    content      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at   REAL NOT NULL,
    extracted    INTEGER NOT NULL DEFAULT 0,
    redacted     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id, extracted);

CREATE TABLE IF NOT EXISTS memories (
    id            TEXT PRIMARY KEY,
    pool_id       TEXT NOT NULL,
    session_id    TEXT,
    type          TEXT NOT NULL,   -- fact | preference | decision | event | relation | table | procedure | document
    subject       TEXT NOT NULL DEFAULT '',
    predicate     TEXT NOT NULL DEFAULT '',
    value         TEXT NOT NULL DEFAULT '',
    text          TEXT NOT NULL,
    scope         TEXT,
    valid_from    REAL NOT NULL,
    valid_to      REAL,
    status        TEXT NOT NULL,   -- active | superseded | disputed | quarantined | archived | rejected
    trust         TEXT NOT NULL,   -- high | medium | medium_low | low
    confidence    REAL NOT NULL DEFAULT 0.7,
    importance    REAL NOT NULL DEFAULT 0.5,
    sensitivity   TEXT NOT NULL DEFAULT 'internal',  -- public | internal | confidential | restricted
    owner         TEXT,
    author_agent  TEXT NOT NULL,
    source_type   TEXT NOT NULL,
    superseded_by TEXT,
    flags         TEXT NOT NULL DEFAULT '[]',
    pinned        INTEGER NOT NULL DEFAULT 0,
    access_count  INTEGER NOT NULL DEFAULT 0,
    last_accessed REAL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS memories_key ON memories(pool_id, subject, predicate);
CREATE INDEX IF NOT EXISTS memories_status ON memories(status);

CREATE TABLE IF NOT EXISTS memory_sources (
    memory_id TEXT NOT NULL,
    event_id  TEXT NOT NULL,
    quote     TEXT NOT NULL,
    PRIMARY KEY (memory_id, event_id)
);

-- Every derived item points back at the memory it came from, so forgetting
-- can walk downstream without guessing.
CREATE TABLE IF NOT EXISTS lineage (
    child_kind TEXT NOT NULL,   -- fts | vector | edge | file | file_chunk | typed_row | session_copy | merged
    child_id   TEXT NOT NULL,
    memory_id  TEXT NOT NULL,
    PRIMARY KEY (child_kind, child_id, memory_id)
);
CREATE INDEX IF NOT EXISTS lineage_memory ON lineage(memory_id);

CREATE TABLE IF NOT EXISTS tombstones (
    hash       TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,   -- key | text
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    id         TEXT PRIMARY KEY,
    src        TEXT NOT NULL,
    relation   TEXT NOT NULL,
    dst        TEXT NOT NULL,
    memory_id  TEXT NOT NULL,
    pool_id    TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst);

CREATE TABLE IF NOT EXISTS typed_rows (
    id         TEXT PRIMARY KEY,
    table_name TEXT NOT NULL,
    row        TEXT NOT NULL,   -- JSON object; query with json_extract
    memory_id  TEXT NOT NULL,
    pool_id    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS typed_rows_table ON typed_rows(table_name);

CREATE TABLE IF NOT EXISTS file_chunks (
    id          TEXT PRIMARY KEY,
    memory_id   TEXT NOT NULL,
    path        TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    text        TEXT NOT NULL
);

-- A subagent's scratch pool sees parent memories by reference, so forgets and
-- updates in the parent reach it without copying content.
CREATE TABLE IF NOT EXISTS pool_members (
    pool_id   TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    PRIMARY KEY (pool_id, memory_id)
);
CREATE INDEX IF NOT EXISTS pool_members_memory ON pool_members(memory_id);

-- Conflicts the memory layer couldn't settle by rule, by evidence, or with its own LLM judgement.
-- Open ones are handed to the calling agent through recall results and the decisions API.
CREATE TABLE IF NOT EXISTS conflicts (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,     -- dispute (equal trust) | override (less trusted source vs. a trusted one)
    memory_ids  TEXT NOT NULL,     -- JSON list; the first is the newcomer
    subject     TEXT NOT NULL DEFAULT '',
    predicate   TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL,     -- open | auto_resolved | resolved | defaulted
    resolution  TEXT,              -- JSON: {action, winner, by, reason}
    attempts    TEXT NOT NULL DEFAULT '[]',  -- JSON: what the resolver tried and why it stopped
    created_at  REAL NOT NULL,
    resolved_at REAL
);
CREATE INDEX IF NOT EXISTS conflicts_status ON conflicts(user_id, status);

CREATE TABLE IF NOT EXISTS consolidation_runs (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    trigger     TEXT NOT NULL,   -- agent | nightly | size | catch_up | manual
    started_at  REAL NOT NULL,
    finished_at REAL,
    stats       TEXT NOT NULL DEFAULT '{}'
);

-- Column registry for typed tables so the consolidator can't drift a table's shape.
CREATE TABLE IF NOT EXISTS table_schemas (
    table_name TEXT PRIMARY KEY,
    columns    TEXT NOT NULL,   -- JSON list of column names
    created_at REAL NOT NULL
);

-- Maps vec0 rowids to the item they embed.
CREATE TABLE IF NOT EXISTS vec_map (
    rowid     INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL,   -- memory | file_chunk
    item_id   TEXT NOT NULL,
    memory_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS vec_map_memory ON vec_map(memory_id);

CREATE TABLE IF NOT EXISTS tasks (
    id         TEXT PRIMARY KEY,
    title      TEXT,
    status     TEXT NOT NULL DEFAULT 'open',   -- open | handed_over | done
    from_agent TEXT,
    to_agent   TEXT,
    packet     TEXT NOT NULL,                  -- JSON handover packet
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS ops (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    hlc        TEXT NOT NULL,
    op         TEXT NOT NULL,   -- create | supersede | dispute | quarantine | confirm | reject | archive | delete
    target     TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    agent      TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS metrics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    op         TEXT NOT NULL,
    path       TEXT,
    agent      TEXT,
    latency_ms REAL NOT NULL,
    tokens_in  INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    llm_tokens INTEGER NOT NULL DEFAULT 0,
    meta       TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);

-- LLM calls that fell back to another model or failed outright (rate limits, outages, bad output).
CREATE TABLE IF NOT EXISTS llm_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT NOT NULL,
    outcome    TEXT NOT NULL,   -- fallback | failed
    model      TEXT,            -- the model that finally answered (null when failed)
    errors     TEXT NOT NULL,   -- JSON list, one entry per hop that didn't answer
    created_at REAL NOT NULL
);

-- Signed, hash-chained deletion receipts. Hashes, ids and probe outcomes only, never the forgotten content.
CREATE TABLE IF NOT EXISTS forget_receipts (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    id         TEXT NOT NULL UNIQUE,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL,
    sig        TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at REAL NOT NULL
);

-- Identifiers and hashes only, never deleted content.
CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent      TEXT,
    action     TEXT NOT NULL,
    target     TEXT,
    detail     TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    text, subject, predicate, value, memory_id UNINDEXED, tokenize = 'porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, chunk_id UNINDEXED, memory_id UNINDEXED, tokenize = 'porter unicode61'
);
