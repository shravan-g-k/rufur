# RUFUR: Knowledge Brain

A memory layer that any agent can use through **MCP** or **REST**. Every memory traces back to its source quote and time. RUFUR handles updates and contradictions, quarantines planted facts, says "I don't know", forgets on request and proves the deletion, and lets agents and subagents share memory safely.

It runs as an Ollama-style local daemon on `127.0.0.1:7437`, which serves the REST API, the MCP endpoint and a visual brain UI. All LLM work runs on **local Ollama models**, with free cloud models as a fallback.

This README is the end-to-end technical reference. The core features each have their own deep dive:

| Feature | Document |
|---|---|
| Short-term memory (STM): capture, lifetime, sweeper | [docs/SHORT_TERM_MEMORY.md](docs/SHORT_TERM_MEMORY.md) |
| Long-term memory (LTM): consolidation, stores, journal, staleness | [docs/LONG_TERM_MEMORY.md](docs/LONG_TERM_MEMORY.md) |
| Retrieval: working set, hybrid search, abstaining | [docs/RETRIEVAL.md](docs/RETRIEVAL.md) |
| Conflict resolution: updates, disputes, decisions | [docs/CONFLICT_RESOLUTION.md](docs/CONFLICT_RESOLUTION.md) |
| Trust, quarantine and planted-memory defence | [docs/TRUST_AND_QUARANTINE.md](docs/TRUST_AND_QUARANTINE.md) |
| Forgetting and deletion proof | [docs/FORGETTING.md](docs/FORGETTING.md) |
| Subagent scratch pools and handover | [docs/SUBAGENT_POOLS.md](docs/SUBAGENT_POOLS.md) |

Hands-on walkthrough: [docs/TRY_IT.md](docs/TRY_IT.md). Benchmark output: [docs/results/](docs/results/).

---

## 1. System overview

```
                 Claude Code hooks      opencode / pi         any MCP or REST client
                        │                     │                         │
                        └──────────┬──────────┴────────────┬────────────┘
                                   ▼                       ▼
                         ┌───────────────────── daemon (uvicorn, 127.0.0.1:7437) ──────────────────┐
                         │  /v1 REST (FastAPI)     /mcp (MCP, stateless HTTP)     / (React UI)      │
                         │                     api/rest.py   api/mcp_server.py                      │
                         │                              │                                           │
                         │                      MemoryService  (service.py)                         │
                         │      one facade: identity, ACLs, sessions, write, read, forget, pools    │
                         │   ┌────────────┬─────────────┬───────────────┬──────────────┬─────────┐  │
                         │   │ write/     │ read/       │ forget/       │ pools.py     │ stm.py  │  │
                         │   │ extract    │ search      │ engine        │ handover.py  │ sched-  │  │
                         │   │ pipeline   │ recall      │ probes        │              │ uler.py │  │
                         │   │ conflict   │ rerank      │ receipts      │              │         │  │
                         │   │ resolve    │ working_set │ purge         │              │         │  │
                         │   │ consolidate│ verify      │ tombstone     │              │         │  │
                         │   └────────────┴─────────────┴───────────────┴──────────────┴─────────┘  │
                         │            Ledger (ledger.py)  ·  Database (db/conn.py)                  │
                         │            LLM (llm.py)  ·  Embedder (embed.py)                          │
                         └───────────────────────────────┬──────────────────────────────────────────┘
                                                         ▼
                                  ~/.rufur/rufur.db  (SQLite: WAL + FTS5 + sqlite-vec)
                                  ~/.rufur/files/    (procedure/document markdown)
                                  ~/.rufur/receipt.key (HMAC key for deletion receipts)
```

Design decisions that shape everything else:

- **One daemon is the only writer.** All access goes through `Database`, which serialises statements with an `RLock` on a single SQLite connection. The stdio MCP server (`rufur mcp`) is a thin `RemoteBackend` that forwards to the daemon over REST, so there is never a second writer.
- **SQL rows and vectors live in one SQLite file** and are written in one transaction (`Ledger.index_vector`). FTS5 handles keyword search, [sqlite-vec](https://github.com/asg017/sqlite-vec) (`vec0`, cosine) handles vectors.
- **The ledger is authoritative; everything else is a derived view.** `events`, `memories`, `memory_sources` and `lineage` are the record. FTS rows, vectors, graph edges, typed rows, file chunks and journals are derived and each registers itself in `lineage`, so deletion can walk downstream without guessing.
- **LLMs propose, rules decide.** Every model output is validated, and every LLM step has a deterministic fallback. RUFUR keeps working (more conservatively) with the LLM down.
- **Everything is traceable.** Each memory carries its source event, a verbatim quote, the authoring agent, the time, a validity window and a trust level.

---

## 2. Repository layout

```
src/rufur/
  app.py                 wires service + MCP + REST into one ASGI app
  config.py              Config dataclass, TOML + env layering, per-role LLM provider chains
  service.py             MemoryService: the facade every API calls (agents, pools, sessions, write, read, forget, STM, metrics)
  ledger.py              low-level writes: events, memories, sources, lineage, vectors, edges, typed rows, files, ops, audit, metrics
  db/
    schema.sql           tables, FTS5 virtual tables
    conn.py              SQLite connection, migrations, vec0 table, secure_delete, compact/scrub
  write/
    chunk.py             sentence chunking, salience gate, sensitivity patterns
    extract.py           LLM claim extraction (flat rows, verbatim quotes)
    pipeline.py          WritePath.commit: trust → tombstone → conflict → placement
    trust.py             trust order/weights, instruction-like detector, clearance
    importance.py        initial importance and retention score
    conflict.py          candidate finding + LLM classify (duplicate/update/context/coexist/unclear)
    resolve.py           conflict resolver: rules → LLM → calling agent → default
    review.py            LLM review of held (quarantined) memories
    consolidate.py       STM → LTM promotion, nightly journal
  read/
    search.py            Filter (ACL before ranking) + FTS5/vector hybrid search with RRF
    recall.py            scoring, abstain, verify band, token packing, rendering
    rerank.py            MiniLM (fast) and Qwen3 (deep) cross-encoder rerankers
    verify.py            LLM check for borderline recalls
    working_set.py       session working set (LRU, in-RAM embeddings)
  forget/
    engine.py            target finding, expand, redact, purge, verify, receipt
    probes.py            before/after deletion probes
    purge.py             lineage-driven hard delete
    tombstone.py         salted-hash tombstones
    receipts.py          signed, hash-chained deletion receipts
  stm.py                 STM expiry extension, sweeper, TTL tuning
  scheduler.py           background thread: idle extraction, sweep, nightly consolidation
  pools.py               subagent scratch pools (capability addresses)
  handover.py            task handover packets
  llm.py                 role-routed structured LLM calls with provider fallback
  embed.py               Ollama embedder, offline hash embedder
  dashboard.py, raw.py   read models for the UI and the raw database browser
  events_bus.py          in-process pub/sub feeding the SSE stream
  daemon/cli.py          `rufur` CLI (typer)
integrations/            Claude Code plugin (hooks + skill + MCP), opencode, pi
ui/                      React + Vite + shadcn dashboard, built into src/rufur/static
tests/                   offline pytest suite (fake LLM, hashing embedder, adjustable clock)
scripts/                 demos, evals and benchmarks
docs/                    feature docs, walkthroughs, benchmark results
```

---

## 3. Data model

All tables are in [`src/rufur/db/schema.sql`](src/rufur/db/schema.sql). Columns added later are applied with `ALTER TABLE` from `MIGRATIONS` in `db/conn.py`, so older databases keep working.

### Authoritative

| Table | Purpose |
|---|---|
| `events` | Raw captured messages: `role`, `source_type` (user/document/agent/tool/web), content, content hash, `extracted`, `redacted`. |
| `memories` | The claims. Key columns below. |
| `memory_sources` | `(memory_id, event_id, quote)`: the verbatim evidence for each memory. |
| `lineage` | `(child_kind, child_id, memory_id)`: every derived row (`fts`, `vector`, `edge`, `typed_row`, `file`, `file_chunk`, `journal`) points back at its memory. |
| `ops` | Append-only operation log (create, supersede, dispute, quarantine, confirm, reject, archive, promote, delete) with a hybrid-logical-clock string. |
| `audit` | Who did what, with ids and hashes only, never deleted content. |

Key `memories` columns:

| Column | Meaning |
|---|---|
| `type` | fact · preference · decision · event · relation · table · procedure · document |
| `subject`, `predicate`, `value` | The normalised key and value. `text` is a self-contained sentence. |
| `tier` | `stm` or `ltm`. |
| `store` | Where it was filed: profile · fact · table · graph · procedure · journal · discard. |
| `status` | active · superseded · disputed · quarantined · archived · rejected |
| `trust` | high · medium · medium_low · low, set from where the quote came from. |
| `valid_from`, `valid_to` | The validity window. An update closes the old row instead of deleting it. |
| `volatility`, `revalidate_at` | static / slow / volatile, and the date after which the fact is flagged stale. |
| `expires_at` | STM expiry. `NULL` once promoted to LTM. |
| `sensitivity` | public · internal · confidential · restricted, compared against an agent's clearance. |
| `corroboration`, `confidence`, `importance` | Independent-source count, certainty, and importance used for ranking and retention. |
| `superseded_by`, `flags`, `pinned` | History link, free-form flags (`instruction_like`, `user_confirmed`, `conflicts_with:<id>`...), and a never-discard switch. |
| `target_pool` | The long-term pool an STM item will be promoted into. |

### Derived views

| Table | Built from | Used by |
|---|---|---|
| `memories_fts`, `chunks_fts` | memory text, document chunks | keyword search (BM25, porter stemming) |
| `vec_items` (vec0) + `vec_map` | embeddings of memories and chunks | vector search; `vec_map` ties a vector rowid to its item and memory |
| `entities`, `edges` | `relation` memories | the knowledge graph |
| `typed_rows`, `table_schemas` | `table` memories | structured records (JSON, queried with `json_extract`); `table_schemas` freezes each table's columns |
| `file_chunks` + files on disk | `procedure` and `document` memories | whole-procedure retrieval |

### Access, coordination and bookkeeping

| Table | Purpose |
|---|---|
| `agents` | Name, `user_id`, key hash, `clearance`, `trust` of what it authors, `quarantined`. |
| `pools` | `long` (shared LTM), `stm` (one per user), `scratch` (one per subagent). Holds the ACL JSON. |
| `pool_members` | A scratch pool's by-reference view of parent memories. |
| `sessions` | One per agent conversation; links the events pool and the target LTM pool. |
| `conflicts` | Disagreements and how they were or weren't settled. |
| `consolidation_runs` | One row per STM→LTM run with stats. |
| `tombstones`, `forget_receipts` | Forgetting machinery. |
| `tasks` | Handover packets. |
| `metrics` | Latency, tokens and per-call metadata, which drive the UI and `tune-stm`. |

---

## 4. Identity, tenancy and access control

- **Agents** are rows in `agents`. `rufur agent add NAME` issues a key (`rk_...`, stored as a SHA-256). Send `Authorization: Bearer <key>`, or `X-Rufur-Agent: <name>` when `require_auth` is off.
- **Users own STM.** Every agent belongs to a `user_id` (default `me`). All of a user's agents (Claude Code, opencode, pi) share one STM pool `stm:<user>`, which is why a fact told to one agent is available to the others within seconds.
- **Pool ACLs** are `{"read": [...], "write": [...]}` with entries `"*"`, an agent name, or `"user:<id>"`. Subagent identities (user `scratch:<pool>`) never match `"*"`.
- **Clearance.** Each agent has the highest sensitivity it may read. Sensitivity is set at write time: the extractor's guess is a hint, and regex patterns for keys, credentials, health, finance, emails and phone numbers set a floor.
- **Filter before ranking.** `read/search.py::Filter.sql` bakes pool visibility, status, sensitivity, trust, and validity into every SQL query. A memory the caller may not see never becomes a candidate, so it cannot influence scores or leak through "I found something but can't show it" behaviour. Abstaining returns identical text whether nothing exists or the caller may not see it.
- **Privacy-preserving forget.** If the user asks an agent to forget something above its clearance, the agent sees `[restricted memory, text hidden]` but can still delete it (see [FORGETTING.md](docs/FORGETTING.md)).

---

## 5. The write path

```
 message ─▶ events row ─▶ salience gate ─▶ chunk ─▶ LLM extract ─▶ quote check ─▶ WritePath.commit ─▶ STM
 (hook /     (verbatim)    (regex, no LLM)  ≤1200   (local 7B)      (verbatim)       trust, tombstone,
  log_event)                                chars                                    conflict, placement
```

**1. Capture (`service.log_event`).** Every message is stored verbatim in `events` with `source_type` derived from its role (`user`→user, `assistant`→agent, `tool`→tool, `document`→document, `web`→web). Messages that cannot hold a durable fact (chit-chat, requests, repeats within a session) are marked extracted immediately and never reach a model.

**2. Triggering extraction.** Extraction runs for a session when:
- `checkpoint_every` (6) salient events are pending,
- the session goes quiet for `extract_idle_s` (30 s; the scheduler's per-tick job),
- the session ends (`session_end`),
- the sweeper finds a session quiet for `session_idle_extract_min` (10 min) that nobody ended.

All extraction runs on a single worker thread (`ThreadPoolExecutor(max_workers=1)`), so it never overlaps other background jobs.

**3. Chunk and gate (`write/chunk.py`).** Text is split into sentence-bounded chunks of at most 1200 characters (verbatim slices). `is_salient` keeps only chunks that look like statements about the user, preferences, decisions, changes, dates, procedures, factual claims or secrets, and drops questions and requests unless they also contain a statement ("can you note that I moved to Pune?"). Assistant text passes only when it states an outright decision.

**4. Extract (`write/extract.py`).** Chunks go in batches of about 1400 characters (about one chunk per call) to the `extract` LLM role, which returns compact rows `[chunk, type, subject, predicate, value, (sensitivity)]` rather than sentences. The `text` is built in code (`FlatClaim.text`), because on a local 7B model decoding time is the cost and a row of five strings is about 3x cheaper than a JSON object with a sentence. A response with any malformed row is rejected and the next model in the chain gets a go. The **quote** is the source chunk itself, checked to be a verbatim substring of the event (`quote_in`).

**5. Commit (`write/pipeline.py::WritePath.commit`).** One validated claim becomes ledger state:

1. **Alias normalisation.** If the subject is the user's own name, it becomes `user`, so `rahul sharma|age` and `user|age` share one key space.
2. **Tombstone check.** A claim whose text or key hashes to a tombstone is dropped (`outcome: tombstoned`): forgotten facts don't come back.
3. **Trust** is set from `source_type` (agent writes use the agent's configured trust). **Flags**: `instruction_like` if the claim or its quote matches steering language; `agent_quarantined` if the agent is held. Low-trust claims start `quarantined`, and so do instruction-like ones unless the user said them. Held and rejected memories record why in `status_reason`.
4. **Conflict candidates**: existing live memories with the same `(subject, predicate)` key, plus vector neighbours at cosine distance ≤ 0.15, across the user's STM and the target LTM pool.
5. **Classification** of each candidate (duplicate / update / context / coexist / unclear / unrelated), detailed in [CONFLICT_RESOLUTION.md](docs/CONFLICT_RESOLUTION.md).
6. **Insert** the memory with `tier='stm'`, `expires_at = now + 72h`, a volatility and `revalidate_at`, an initial importance by type, then attach sources, embed it, and place derived views (vector always; graph edge for `relation`; typed row for `table`; markdown file + chunks for `procedure`/`document`).
7. **Apply outcomes**: supersede or dispute older rows, record ops and audit rows, and open a `conflicts` row if the rules could not settle a disagreement.

**6. Settle (`service._settle`).** Right after a commit, the memory layer tries to resolve any new conflict itself (`resolve.auto_resolve`) or, for a held memory, reviews it with the review LLM (`review.review`).

**7. Guard (`service._guard`).** After each write batch, an agent writing more than 60 memories/min (not counting claims from the user's own messages) or making more than 5 override attempts/hour is quarantined, along with everything it wrote.

**Explicit `remember`.** Agents can write directly. Without a `subject`/`predicate`, the text goes through the same extractor with the salience gate off. `source="user"` is believed only if a logged user turn from that agent in the last 24 h contains the text; otherwise it is downgraded to `document` trust with an `unverified_user_claim` flag. `long_term=true` consolidates the new memory immediately.

---

## 6. Memory tiers and lifecycle

```
  extract / remember
         │
         ▼
   STM (tier=stm, 72h base, sliding)  ──── used? ──▶ expiry pushed +24h (hard cap 7 days from creation)
         │
         │ consolidate (agent call, nightly 03:00, STM size, expiry, catch-up on start)
         ▼
   LTM (tier=ltm, same id, same lineage)  stores: profile · fact · table · graph · procedure · journal
         │
         ├─ superseded / disputed / rejected   (status changes; history kept)
         ├─ stale after revalidate_at          (still returned, tagged and down-ranked)
         └─ forgotten                          (hard delete + tombstone + receipt)
```

Short reference (full detail in the dedicated docs):

- **STM** ([SHORT_TERM_MEMORY.md](docs/SHORT_TERM_MEMORY.md)): shared per user; lives 72 h from creation; each recall that returns an item extends it to at least 24 h from the use, capped at 168 h from creation. The sweeper consolidates expiring items first, archives what consolidation discarded, hard-deletes archived items after a 24 h grace, closes forgotten scratch pools and drops old unreferenced events. `rufur tune-stm` suggests a base lifetime from the 95th percentile age at which STM items are actually reused (clamped to 48–96 h).
- **LTM** ([LONG_TERM_MEMORY.md](docs/LONG_TERM_MEMORY.md)): consolidation asks an LLM which store each STM item belongs in, validates the answer, falls back to rules, and promotes **in place**. It also merges duplicates, sweeps for contradictions across agents, and writes a daily journal digest.

---

## 7. The read path

Recall is tiered; each tier is cheaper than the next ([RETRIEVAL.md](docs/RETRIEVAL.md)):

1. **Working set.** At session start (`bootstrap`) RUFUR preloads profile memories, the last day of STM, and project-relevant LTM into a per-session LRU with embeddings. A later recall is answered from it, with no search, if one of its items clearly matches. Items changed since (forgotten, superseded, quarantined) are filtered out at lookup time.
2. **Fast hybrid search** (`read/search.py`): FTS5 BM25 over memories and over document chunks, plus sqlite-vec KNN, each under the ACL filter, fused by reciprocal rank (`k=60`).
3. **Scoring** (`read/recall.py::fast_recall`): relevance blends cosine and IDF-weighted word coverage (optionally blended with a MiniLM cross-encoder score), then multiplies by recency (30-day half-life), trust weight, importance, a x1.1 STM boost, x0.9 for disputed, x0.8 for stale, and a small corroboration bonus.
4. **Verify band.** If top relevance is in `[0.2, 0.6)`, the verifier LLM picks which of the top candidates actually answer the question; its picks pass regardless of lexical score.
5. **Abstain.** Below the threshold (0.42 without a verifier) RUFUR returns `abstained: true` and "No stored memory answers this."
6. **Pack.** Keep items within 70% of the best relevance, seed with the best one, fill greedily by score-per-token into the token budget (default 800).
7. **Render** as `<memory id=... trust=... since=...>` records under a header that says they are data, not instructions. Disputed, stale, superseded and scoped items carry attributes. Open conflicts touching the returned items are appended as `decisions_needed`.
8. **`recall_past`** drops the current-only filter, includes superseded and archived items, supports `since`/`until`, and labels old items `current="false"`.

Recall has side effects by design: it bumps `access_count` and `last_accessed` (feeding retention scoring) and extends the expiry of STM hits.

---

## 8. Contradictions, planted facts and review (summary)

Full detail: [CONFLICT_RESOLUTION.md](docs/CONFLICT_RESOLUTION.md) and [TRUST_AND_QUARANTINE.md](docs/TRUST_AND_QUARANTINE.md).

- **Stale:** every memory records when it was learned and when it was true. An update closes the old version instead of deleting it. Changeable facts get a revalidation date; after it passes the fact is still returned, tagged `stale="needs confirmation"`, and ranked lower.
- **Contradictory:** RUFUR settles what it can, then hands the rest to the calling agent. No UI step.
  1. Rules: a tool or web claim never beats an existing memory, an earlier user confirmation wins, clearly better-corroborated claims win. An LLM label of "update" counts only when the *source text* signals a change.
  2. The resolver LLM re-reads both source messages. Its verdict counts only if it's confident, and it can never give the win to a less trusted source.
  3. Otherwise the calling agent decides via `decisions_needed` / `resolve_conflict`. It asks the user only when `needs_user` is true, and RUFUR accepts `decided_by="user"` only if the user just wrote to that agent.
  4. After 7 days the more trusted (then newer) version wins; the loser stays in history.
- **Planted:** trust comes from where the quote physically sits (user turn, document, tool or web output); the LLM can never raise it. Low-trust and instruction-like content is quarantined until an independent source corroborates it, the review LLM rejects it, or the user approves it. A burst-writing or override-spamming agent is quarantined automatically. Recalled memories are always wrapped as data.

---

## 9. Forgetting (summary)

Full detail: [FORGETTING.md](docs/FORGETTING.md).

`forget` by id deletes immediately. `forget` by query returns candidates first (key match, a Qwen3 reranker over questions derived from the request, an LLM judge), then deletes on confirmation. A forget:

1. expands the targets to every version and verbatim copy,
2. builds and runs **probes before** deleting (each must find the content, a positive control),
3. writes salted-hash **tombstones**,
4. **redacts** the content from events and handover packets and drops related conflicts,
5. **purges** the memories and every derived row through `lineage`, then truncates the WAL (SQLite `secure_delete`, FTS5 `secure-delete`),
6. runs the **probes after** (ids, text scans of every store, recall through every read path, nearest-neighbour search with the memory's own vector, raw byte scan of the DB, WAL and files), escalating to `VACUUM` only if the byte scan still finds residue,
7. issues an **HMAC-signed, hash-chained receipt** with hashes, ids and probe outcomes, never the content.

`rufur verify-forgetting` re-checks the whole receipt chain; the nightly sweeper also re-checks for forgotten content that resurfaced and removes it.

---

## 10. Subagents and handover (summary)

Full detail: [SUBAGENT_POOLS.md](docs/SUBAGENT_POOLS.md).

- `pool_spawn(purpose, needs)` creates a scratch pool, seeds it **by reference** (`pool_members`) with the matching parent memories within a token budget, creates a scoped subagent identity, and returns a capability address `mem://<pool>/<secret>` to put in the subagent's prompt.
- The subagent uses `pool_search`, `pool_remember` and `pool_fetch` with that address; it can touch nothing else.
- `pool_close` files the subagent's useful writes back into the parent's STM **through the normal write path** (trust, quarantine and conflict checks apply again), discards the rest, and revokes the key. The sweeper closes pools nobody closed.
- `handover_create` / `handover_resume` pass a task between agents as a packet (goal, steps done/pending, decisions, open questions, memory ids) with provenance, redacted if its memories are later forgotten.

---

## 11. Background work

`Scheduler` (`scheduler.py`) is one daemon thread inside the server. Jobs are submitted to the service's single worker, so they never overlap with extraction.

| Cadence | Job |
|---|---|
| every 15 s | extract sessions that went quiet for 30 s |
| every 15 min (checked each minute) | `sweep`: STM lifecycle, decision defaults, resurfaced-forgotten check |
| after 03:00 local, once per day | nightly consolidation plus yesterday's journal |
| on daemon start | catch-up consolidation if the last run is older than 20 h |
| when STM is large | size-triggered consolidation (more than 200 pending items or 300 events since last run) |

On start the daemon also warms the embedder and reranker and back-fills embeddings for memories written while the embedder was down (`reconcile_embeddings`).

---

## 12. LLM and embedding layer

- **Roles, not models.** Code asks for a role (`extract`, `classify`, `consolidate`, `resolve`, `review`, `verify`, `forget`, `probe`, `answer`). Each role has a provider chain in `config.py`/`config.toml`; defaults are local `qwen2.5:7b-instruct`, then Ollama cloud `gpt-oss:120b-cloud`, then Groq `llama-3.3-70b-versatile` (used only if `GROQ_API_KEY` is set). `probe` uses `qwen3.5:4b` then `qwen2.5:7b-instruct`.
- **Structured output only.** Each call passes a Pydantic JSON schema (`format` for Ollama, `response_format` for OpenAI-compatible). Invalid output is retried once on the same hop, then the next hop is tried. A connection error marks that hop down for 30 s so a stopped Ollama doesn't add a timeout to every call.
- **`LLMUnavailable` is a first-class outcome.** Every caller has a deterministic fallback: extraction stays queued, classification returns "unclear", consolidation uses rules, review holds the memory, forget uses key matches plus plain recall.
- **Embeddings:** Ollama `nomic-embed-text` (768-d) with task prefixes, or a deterministic hashing embedder for tests and fully offline installs (`embed_provider="hash"` or `RUFUR_OFFLINE=1`). The embedder name is stored in the database; mixing embedders in one index is refused at startup.
- **Rerankers (optional, `uv sync --extra rerank`):** MiniLM cross-encoder on every recall; Qwen3-Reranker-0.6B for forget-by-query. Models load lazily; without them everything falls back to unreranked behaviour.

---

## 13. Interfaces

### MCP tools and REST endpoints

| MCP tool | REST |
|---|---|
| `remember` (`long_term=true` files it immediately) | `POST /v1/memories` |
| `recall` | `POST /v1/recall` |
| `recall_past` | `POST /v1/recall/past` |
| `inspect` | `GET /v1/memories/{id}` |
| `confirm_memory` | `POST /v1/memories/{id}/confirm` |
| `forget`, `verify_forgetting` | `POST /v1/forget`, `POST /v1/forget/verify` |
| `consolidate` | `POST /v1/consolidate` |
| `pending_decisions` / `resolve_conflict` | `GET /v1/decisions`, `POST /v1/decisions/{id}` |
| `pool_spawn` / `pool_search` / `pool_remember` / `pool_fetch` / `pool_info` / `pool_close` | `POST /v1/pools/spawn`, `/v1/pool/*`, `POST /v1/pools/{id}/close` |
| `log_event`, `session_start` / `session_end` | `POST /v1/events`, `POST /v1/sessions`, `POST /v1/sessions/{id}/end` |
| `handover_create` / `handover_resume` | `POST /v1/tasks/{id}/handover`, `POST /v1/tasks/{id}/resume` |
| `list_pools` | `GET /v1/pools` |

Used by the hooks and the UI: `POST /v1/bootstrap` (working set), `GET /v1/graph`, `/v1/quarantine`, `/v1/metrics/summary`, `/v1/audit`, `/v1/consolidation/runs`, `/v1/stm/stats`, `/v1/scratch-pools`, `/v1/doctor`, `/v1/dashboard/*`, `/v1/raw/*`, `/v1/forget/receipts`, and `/v1/stream` (server-sent events from `EventBus`). Interactive API docs: `/docs`.

The MCP endpoint (`/mcp`) runs in stateless JSON mode, so no session state has to survive daemon restarts. The MCP server's `INSTRUCTIONS` string teaches agents the workflow.

### CLI (`rufur`)

`serve`, `start`, `stop`, `status`, `logs`, `open`, `mcp` (stdio bridge), `consolidate [--journal]`, `sweep`, `tune-stm`, `doctor`, `verify-forgetting`, `agent add|list`, `install-service` (launchd).

### Integrations

- **Claude Code plugin** (`integrations/claude-code`): `hooks/rufur_hook.py` (stdlib only, fails open) handles `SessionStart` (bootstrap and inject the working set), `UserPromptSubmit` (capture the prompt, inject unseen relevant memories), `PostToolUse` for `WebFetch|WebSearch` (capture as low trust), `Stop` (capture the reply), `SubagentStop` (close idle pools), `SessionEnd`. Ships the `rufur-memory` skill and registers the MCP server.
- **opencode** (`integrations/opencode`): MCP config plus `AGENTS.md` instructions and a plugin.
- **Antigravity** (`integrations/antigravity`): `mcp_config.json` (stdio bridge `rufur mcp` as agent `antigravity`) to merge into `~/.gemini/config/mcp_config.json`, plus `GEMINI.md` rules for `~/.gemini/GEMINI.md`.
- **pi** (`integrations/pi`): no MCP, so a skill with `scripts/rufur.py` plus an extension that recalls and captures automatically.

### The UI (`ui/`)

React 18 + Vite + Tailwind + shadcn, built into `src/rufur/static` and served by the daemon at `/`. Pages: Overview, Sessions & pools, Tokens, Forgetting, Review (held memories and open decisions), Agents, Raw (read-only browser over every table, opened with a separate `mode=ro` connection).

---

## 14. Configuration

Values come from `~/.rufur/config.toml` (or `$RUFUR_HOME/config.toml`) over the defaults in `src/rufur/config.py`. `RUFUR_HOME`, `RUFUR_PORT` and `RUFUR_OFFLINE=1` override per process.

| Group | Settings (default) |
|---|---|
| Write | `checkpoint_every` 6, `extract_idle_s` 30, `chunk_chars` 1200, `extract_batch_chars` 1400 |
| Read | `default_token_budget` 800, `abstain_threshold` 0.42, `verify_band` (0.2, 0.6), `recency_half_life_days` 30, `candidate_k` 40, `rerank_top_n` 20 |
| STM | `stm_base_ttl_h` 72, `stm_extend_h` 24, `stm_max_ttl_h` 168, `stm_grace_h` 24, `sweep_every_min` 15 |
| Consolidation | `nightly_hour` 3, `catch_up_after_h` 20, `size_trigger_items` 200, `size_trigger_events` 300, `consolidate_batch` 25 |
| Conflicts | `auto_resolve_confidence` 0.8, `decision_default_after_days` 7, `user_presence_window_min` 30 |
| Guard | `max_writes_per_min` 60, `max_override_attempts_per_h` 5 |
| Forgetting | `forget_rerank_floor` 0.05, `forget_exhaustive_max` 200, `forget_judge_max` 30, `archive_below` 0.15, `retention_days` 90 |
| Pools | `scratch_ttl_min` 120 |
| Working set | `working_set_budget` 600, `prompt_recall_budget` 300 |

---

## 15. Quick start

```bash
# prerequisites: uv, Ollama running, and these models pulled
ollama pull qwen2.5:7b-instruct && ollama pull nomic-embed-text && ollama pull qwen3.5:4b

uv sync
cd ui && npm install && npm run build && cd ..   # builds the brain UI into src/rufur/static

uv run rufur start      # background daemon (also runs the STM sweeper and nightly consolidation)
uv run rufur status     # daemon, Ollama, and which model each role will use
uv run rufur open       # open the brain UI
```

Use it from Claude Code:

```
/plugin marketplace add /path/to/rufur
/plugin install rufur@rufur
```

The hooks talk to `http://127.0.0.1:7437`; set `RUFUR_URL`, `RUFUR_AGENT` or `RUFUR_API_KEY` to change that. opencode: merge `integrations/opencode/opencode.json` into your config and add `AGENTS.md` to your instructions. pi: copy `integrations/pi/extensions/rufur.ts` into `~/.pi/agent/extensions/` and `integrations/pi/skills/rufur-memory` into `~/.pi/agent/skills/`.

---

## 16. Development and testing

```bash
uv run pytest                                  # offline: fake LLM, hashing embedder, adjustable test clock
uv run python scripts/demo_scenario.py         # end to end against the running daemon (real models)
uv run python scripts/baseline_compare.py      # full history vs plain vector RAG vs RUFUR
uv run python scripts/llm_bakeoff.py           # compare local models for extraction
uv run python scripts/forget_bench.py          # forgetting at 1k / 5k / 20k memories
uv run python scripts/retrieval_eval.py        # retrieval quality
uv run python scripts/storage_choice.py        # which store fits which knowledge
uv run python scripts/thirty_sessions.py       # thirty-session soak
cd ui && npm run dev                           # UI dev server, proxies /v1 to the daemon
```

The test suite is fully offline: `FakeLLM` (a handler returning canned JSON per role), `HashEmbedder`, and `textutil.CLOCK_OFFSET` to move time forward and exercise expiry and revalidation. Test files: `test_core` (provenance, abstaining, supersede, quarantine, forget, ACLs, handover), `test_v2` (STM expiry, consolidation, working set, scratch pools, reranker, REST/MCP parity), `test_conflicts` (resolver), `test_extraction` (chunking and extraction), `test_mcp`, `test_hook`, `test_dashboard`.

Measured results (`docs/results/`): `storage_choice.md` explains why different knowledge types use different stores (profile in the prompt, facts in hybrid search, tables and graph in code, procedures as files, history in the ledger); `retrieval_eval.md` and `thirty_sessions.md` cover recall quality and multi-session behaviour. `scripts/forget_bench.py` completes 20/20 deletion requests end to end at 1k, 5k and 20k memories, verified by a different agent through recall, `recall_past` and a handover, with erasure taking 3–24 ms versus 10–167 ms for the per-forget `VACUUM` it replaced.
