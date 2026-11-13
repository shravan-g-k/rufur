# Forgetting, and proving it

"Forget my phone number" is a promise that the number is gone from every place RUFUR keeps it, and that it can be shown. RUFUR does not rely on a delete returning success. It checks the store before and after, and signs the result.

Code: `forget/engine.py` (orchestration), `forget/probes.py` (verification), `forget/purge.py` (deletion), `forget/tombstone.py`, `forget/receipts.py`, `db/conn.py` (`secure_delete`, `scrub`, `compact`), `service.py` (`forget`, `_masked`, `auto_forget`), `stm.py` and `scheduler.py` (routine expiry).

There are two kinds of removal, and they behave differently:

| | **User forget** | **Routine expiry / merge** |
|---|---|---|
| Triggered by | the user's request (`forget`) | STM sweeper, consolidation merge, `auto_forget` |
| Tombstone | yes: the fact cannot be re-learned | no: it may be learned again |
| Verified and receipted | yes | no |
| Cascades to daily journals | yes | no |

## 1. The flow

```
 request ─▶ find targets ─▶ expand ─▶ build probes ─▶ probe BEFORE ─▶ tombstone ─▶ redact ─▶ purge
                                                                                              │
 receipt ◀─ probe AFTER (vacuum only if bytes remain) ◀─ scrub (WAL checkpoint+truncate) ◀──────┘
```

### 1.1 Finding targets

`MemoryService.forget` takes `memory_id`, `memory_ids` or a `query`.

- **By id**: deleted immediately (after permission checks).
- **By query**: returns `confirm_required` with candidates, a preview of what would go, and what else mentions the value, unless `confirm=true`. The agent confirms with `memory_ids=[...]` for the ones the user means.

Finding candidates for a query is deliberately recall-first, because missing a copy leaves what the user asked to erase:

1. **Exact key match** (`key_match`) before fuzzy search. A request names a subject ("contact 12's phone", "my phone" → `user`); memories under that subject whose predicate is named match first. "contact 12" must never resolve to contact 120 or a similar-looking neighbour.
2. **Deep rerank** (`find_targets`). The request is turned into 3-5 *questions whose answers are the information to erase* ("forget my health" → medication, doctor, allergies, conditions) plus the request's own topic, rewritten from "I/my" to "the user". A cross-encoder reranker (Qwen3-Reranker-0.6B) scores each candidate memory against each question, keeping the best score, because rerankers judge "does this answer the question?" well and "is this about the topic?" badly.
   - Up to `forget_exhaustive_max` (200) visible memories are **all** scored (exhaustive); more than that, a wide hybrid search supplies the pool.
   - Candidates below `forget_rerank_floor` (0.05) are dropped unless they are key matches; at most `forget_judge_max` (30) go on.
3. **LLM judge** (role `forget`). It decides per candidate: forget (answers a question, or lets someone work it out, including older or changed versions) or keep (shares words or subject only). A candidate the judge skipped is **kept**: deleting needs a positive decision.
4. Without a reranker, it falls back to key matches plus plain fast recall and the agent picks.

### 1.2 Expanding to everything that states the fact (`expand`)

For each target, RUFUR walks to:

- the supersede chain (what it replaced and what replaced it),
- every other version under the same `(subject, predicate)` key in the same pools, *unless* the attribute is list-like (forgetting one goal must not wipe the others),
- verbatim copies of the same text in the pools the fact lives in.

So "forget my phone number" removes every number the attribute ever held. `preview` shows this without removing anything.

Other live memories that merely *mention* the value are not deleted automatically (they are different facts) but are returned as `also_mentioned_in`. If the user agrees, `cascade_mentions=true` deletes them in the same call. Doing it in one call matters: while any of them survives, the bare value counts as still in use and isn't redacted from events and handover packets.

Daily **journals** that summarised any target are collateral and are deleted too, because the digest's text may contain the fact. They are linked by `lineage(child_kind='journal')` and do not decide what gets redacted or verified.

### 1.3 Probes before

`probes.build` constructs probes **while the content still exists** and runs them. Each probe must find the content now; this is a positive control. A probe that couldn't see the fact proves nothing by coming back empty.

| Probe kind | What it checks |
|---|---|
| `direct` | row-count by id in `memories`, `memory_sources`, `lineage`, `vec_map`, `file_chunks`, `edges`, `typed_rows`, `pool_members`, and id references inside `conflicts.memory_ids` and `tasks.packet` |
| `text` | LIKE scans of each store for the key strings: ledger, FTS, file chunks, graph, typed rows, events, handover packets |
| `recall` | queries (the memory text, "What is the {predicate} of {subject}?", and LLM-written paraphrases, one deliberately indirect) run through `fast_recall` with the **widest possible filter**: all pools, all clearances, all statuses including superseded and archived. This covers `recall`, `recall_past` and every pool |
| `vector` | nearest-neighbour search using each target's **own stored embedding**; a survivor within cosine distance 0.02 is the same text under another id. The vectors are held in RAM only for the after-probe, never persisted |
| `bytes` | raw byte scan (case-insensitive) of the database file, its WAL, and every derived file. Positive by construction, so there is no before-run: the bytes were certainly in the file while the row existed |

"Key strings" are the memories' text, their source quotes (4+ chars), and each bare value of 4+ chars **unless** a surviving memory legitimately contains it.

### 1.4 Erasure

In order, after the before-probes:

1. **Tombstones** (`tombstone.add`): salted SHA-256 hashes of the normalised text and of the `subject|predicate|value` key go in `tombstones`. The salt lives in `meta`. RUFUR remembers *that* something was forgotten, never *what*. The write path checks tombstones, so the same fact from any agent is rejected (`outcome: tombstoned`).
2. **Redact events** (`redact_events`): in every event that is a source or merely mentions the strings, each occurrence is replaced with `[forgotten]` (whitespace-tolerant, case-insensitive), the content hash is recomputed and the event is marked `redacted`. The conversation transcript survives; the fact does not.
3. **Redact handover packets** (`redact_tasks`): forgotten ids are removed from `memory_ids` and the text is scrubbed from every field and the title.
4. **Drop conflicts** that reference the memories.
5. **Purge** (`purge_memories`), in one `BEGIN IMMEDIATE` transaction: walk `lineage` and delete each derived item by kind (FTS row, vector and `vec_map`, edge, typed row, file chunk and its FTS row, the file on disk), then a belt-and-braces delete by `memory_id` for rows written without lineage, then `memory_sources`, `lineage`, `pool_members`, unlink supersede chains, delete the `memories` rows, and drop `entities` no edge references any more (names are content too).
6. **Ops and audit** rows (ids and counts only) are written, including `delete` ops so sync replicas apply the delete first.
7. **Scrub**: `PRAGMA wal_checkpoint(TRUNCATE)`.

### 1.5 Why it is fast: erasure cost scales with what is deleted

Forgetting does **not** rewrite the database. Three SQLite features make deletion physical at delete time:

- `PRAGMA secure_delete=ON`: freed pages are overwritten with zeros.
- FTS5 `secure-delete`: deleted terms are removed from index segments immediately rather than lingering until a merge (set on both FTS tables at startup).
- WAL checkpoint with `TRUNCATE`: pushes the zeroed pages out of the write-ahead log and truncates it.

`scripts/forget_bench.py` measured erasure at 3-24 ms against 10-167 ms for the per-forget `VACUUM` it replaced.

### 1.6 Probes after, and the vacuum escalation

The same probes run again; every informative one must now find nothing. `secure_delete` zeroes freed cells but can leave a stale copy of a row that was rewritten in place (for example by status updates) inside a still-live page. So **the byte scan is the oracle**: only if it still finds residue does RUFUR pay for a full `VACUUM` (`db.compact()`: FTS `optimize`, `VACUUM`, WAL truncate), then probes again. The report records `escalated: "vacuum"` when that happened.

### 1.7 The report

`probes.report` summarises per store: probes run, how many were informative, whether the store passed, and any leaks. `completeness = passed / informative`. Probes that found nothing *before* are reported as uninformative and not counted as passes. The report includes the caveat that facts already inside an agent's context window cannot be erased there; RUFUR guarantees it stops returning them.

## 2. Signed, hash-chained receipts

Every user forget issues a receipt (`receipts.issue`). Its body holds: id, agent, time, the **deleted memory ids**, **salted target hashes**, per-store counts, the receipt-safe probe summaries (store, kind, informative, before/after hit *counts*), completeness, the erasure method, the SHA-256 of the database file afterward, and the previous receipt's hash. It never contains the forgotten content or the probe queries.

```
digest = sha256( prev_hash + canonical_json(body) )
sig    = HMAC-SHA256( key, digest )         key = ~/.rufur/receipt.key (0600, outside the database)
```

Because each hash covers the previous one and the key is outside the database, deleting, editing or reordering receipts breaks verification, and nobody with only the database can forge one.

`rufur verify-forgetting` (MCP `verify_forgetting`, `POST /v1/forget/verify`) runs `verify_chain`: recompute every hash and signature, check `prev_hash` linkage from a genesis of 64 zeros, confirm each receipt's memory ids are still absent, and run `recheck`, which finds live memories whose text or key hashes to a tombstone (forgotten content that returned via a sync import, merge or hand edit).

The **nightly sweeper** runs the same `recheck` and deletes anything that resurfaced, auditing it as `forget.resurfaced`.

## 3. Forgetting without reading

If the user asks an agent directly to forget something above that agent's clearance, the agent cannot read it and should not need to. `_masked` returns such candidates as `[restricted memory, text hidden]` with an id, provided the user is present (`_user_present`) and the agent may write to that pool. `_forgettable` lets the id through. A privacy request must not fail because the memory is too private for the agent to read.

## 4. Routine decay (not user forgets)

- **STM expiry**: unreviewed items are consolidated; discards and unreviewed quarantined items are archived, then hard-deleted after 24 h ([SHORT_TERM_MEMORY.md](SHORT_TERM_MEMORY.md)).
- **`auto_forget`** (`POST /v1/maintenance/auto-forget`): archives active, unpinned, non-decision memories whose retention score drops below 0.15, and hard-deletes archived ones past the pool's `retention_days` (default 90), unverified and without tombstones. Decisions and pinned memories are exempt.
- **Superseded memories** are *not* forgotten: they stay in history for `recall_past` until a user asks to forget the fact, which expands to the whole supersede chain.

## 5. What forgetting does not do

- It cannot erase text that an agent already holds in its context window or has written elsewhere.
- It does not delete other memories that merely mention a value unless the user accepts `cascade_mentions`.
- It keeps hashes and ids (receipts, tombstones, audit). Hashes are salted and the content is not recoverable from them; ids are not content.

## 6. Tests and benchmarks

`tests/test_core.py`: `test_forget_is_complete_and_blocks_relearning`, `test_forget_by_query_requires_confirmation`. `tests/test_v2.py`: `test_journal_written_and_removed_by_forget`, `test_forget_by_query_uses_reranker_then_judge`, `test_forget_falls_back_without_reranker`. `scripts/forget_bench.py` runs 20 deletion requests at 1k, 5k and 20k memories, checked by a different agent through recall, `recall_past` and a handover.
