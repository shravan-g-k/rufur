# Long-term memory (LTM) and consolidation

LTM is what STM becomes once it has been reviewed. Nothing is copied: consolidation **promotes a row in place** (`tier: stm → ltm`), so the id, sources, quote, vectors and lineage all stay the same. That is what keeps deletion and provenance simple.

Code: `src/rufur/write/consolidate.py`. Triggers: `src/rufur/scheduler.py`, `MemoryService.consolidate`, `stm.sweep`.

## The stores

After promotion each memory has a `store`, which says how it is organised and retrieved. This follows the measured results in `docs/results/storage_choice.md`: different knowledge fits different storage.

| Store | Holds | Extra structure written | Why |
|---|---|---|---|
| `profile` | Lasting facts and preferences about the user (`subject = 'user'` only) | none; surfaced in the working set at session start | small, needed on almost every request, so cheapest to always inject |
| `fact` | Claims and decisions that aren't profile | vector + FTS | single facts are cheap for hybrid search |
| `table` | Records (invoices, contacts, measurements) | a `typed_rows` JSON row, and a `table_schemas` entry that freezes the table's columns | aggregates and exact lookups are wrong in vector search, right in SQL |
| `graph` | A relationship between two named things | `entities` and an `edges` row `(src, relation, dst)` | multi-hop questions need a walk, not similarity |
| `procedure` | Multi-step how-tos | a markdown file under `~/.rufur/files/` plus chunks with vectors | retrieved whole, not as fragments |
| `journal` | One digest per day | a normal memory with `store='journal'` | answers "what did we do last Tuesday" |
| `discard` | Small talk, transient status, duplicates | none; stays in STM and expires | not worth keeping |

Every derived row is registered in `lineage` the moment it is created.

## When it runs

- An agent calls `consolidate` (usually after finishing a task). `journal=true` also writes today's journal.
- Nightly after `nightly_hour` (03:00 local), once per user per day, with yesterday's journal.
- Catch-up on daemon start if the last run is older than `catch_up_after_h` (20 h).
- A size trigger: more than 200 unconsolidated items or 300 events since the last run.
- Expiry: the sweeper consolidates anything about to expire unreviewed.
- `remember(long_term=true)` consolidates just the new item.

Subagents cannot consolidate (`AccessDenied`); they close their pool instead ([SUBAGENT_POOLS.md](SUBAGENT_POOLS.md)). Each run writes a `consolidation_runs` row with its trigger and stats, shown on the UI and at `GET /v1/consolidation/runs`.

## The algorithm

`consolidate(svc, user_id, trigger, memory_ids=None, journal_day=None)`:

1. **Select** the user's STM rows with `consolidation_run IS NULL` and status `active` or `disputed` (optionally restricted to given ids).
2. **Batch by topic.** Group by `subject`, then pack groups into batches of at most `consolidate_batch` (25) items so related memories are judged together and each LLM call stays small.
3. **Ask** (`_ask`). For each batch the `consolidate` LLM role gets the items, the known table shapes (`KNOWN TABLES`) and up to 40 `EXISTING` LTM memories sharing a subject, and must return one decision per item:
   - `store`: profile / fact / table / graph / procedure / discard
   - `importance` 0-1, and `volatility` static / slow / volatile
   - `merge_with`: the id of an existing memory that says the same thing
   - `relation`, `table_row` or `steps` for the store that needs them

   Only decisions about ids that were actually sent are kept.
4. **Validate** (`validate`). The model never gets the last word. For each item:
   - no decision (LLM down or skipped it) → deterministic rule placement (`_rule_store`): user preferences/facts → `profile`, `relation` → `graph`, `table` → `table`, `procedure` → `procedure`, else `fact`;
   - `discard` of a **protected** item is overridden: decisions, pinned items, disputed items, and anything that superseded another memory are never discarded;
   - `profile` for a non-user subject becomes `fact`;
   - `graph` without a complete `{src, relation, dst}` falls back to rules;
   - `table` needs a non-empty row, and if the table is already registered its columns must be a **subset** of the registered columns (a row that would drift the table's shape falls back to rules);
   - `procedure` needs at least 20 characters of steps;
   - `merge_with` is honoured only if it is among the existing ids shown.

   It can never change `trust` or `status`.
5. **Apply** (`_apply`):
   - **Merge:** if `merge_with` points at a memory with the same normalised value, the sources are attached to the existing memory, its `corroboration` is incremented and `confidence` raised by 0.1, and the new row is purged (no tombstone).
   - **Discard:** the row is marked `store='discard'` and stays in STM until it expires.
   - **Promote:** build whatever the store needs that the row doesn't already have (edge, typed row and schema, document file and chunks), then `UPDATE ... SET tier='ltm', pool_id=<target>, store=..., volatility=..., importance=..., revalidate_at=..., expires_at=NULL, consolidation_run=<run>`. Edges and typed rows are re-pointed at the target pool and an `ops` row `promote` is written.
6. **Contradiction sweep** (`_contradiction_sweep`). Promoted items are compared with older active LTM rows that have the same `(subject, predicate)` but a different value, for example written by another agent in a different session. Both sides are marked `disputed` and an `ops` row recorded. This catches conflicts the write path could not see because the two writes never overlapped in time.
7. **Journal** (optional).
8. **Record** stats (`items`, `promoted`, `merged`, `discarded`, `fallback`, `by_store`, `contradictions`, `llm_tokens`, latency), update the `consolidation_runs` row, emit a metric and publish `consolidation.finished`.

Importance after promotion is the average of the write-time importance and the LLM's: `0.5 * old + 0.5 * proposed`.

## Importance and retention

Write-time importance (`write/importance.py`) is a base by type (decision 0.9, preference 0.75, procedure 0.7, fact 0.6, relation/table 0.55, document 0.5, event 0.4) plus up to 0.1 for length. It scales recall scores by `0.5 + 0.5 * importance`.

`retention_score` blends importance (0.5), recency of last use with a 30-day half-life (0.3) and a log-scaled access count (0.2). `MemoryService.auto_forget` archives active, unpinned, non-decision memories below `archive_below` (0.15) and hard-deletes archived ones past their pool's `retention_days` (90). Decisions and pinned memories are exempt.

## Freshness: volatility and revalidation

Each memory has a `volatility`:

| Volatility | Revalidate after | Typical |
|---|---|---|
| `static` | never | birthday, native language |
| `slow` | 180 days | home city, employer |
| `volatile` | 7 days | current task, this week's plan |

Events are always volatile. The LLM may propose `static`, but `_clamp_volatility` only allows it for identity-like predicates (birth, born, native, name, nationality, blood type) or procedures; anything else is clamped to `slow`, because most facts about a person, project or decision can change.

`revalidate_at = valid_from + days`. After it passes, recall still returns the memory but tags it `stale="needs confirmation"` and multiplies its score by 0.8. `confirm_memory` resets the clock. An update closes the old version (`valid_to`, `status='superseded'`) rather than deleting it, so `recall_past` can still show it.

## The daily journal

`write_journal` writes one digest memory per day (`type='event'`, `store='journal'`, `subject='journal'`, `predicate=<YYYY-MM-DD>`):

- It gathers up to 200 user and assistant events from that day for the user's agents and asks the `consolidate` role for a neutral third-person summary of at most 120 words plus 3-6 topic tags.
- The journal memory has `volatility='static'`, trust `medium_low`, up to 20 source quotes, and an embedding, so it is found by "what did we do last Tuesday".
- It is **linked by lineage** (`child_kind='journal'`) to every memory created that day. Forgetting any of those memories deletes the journal too, because the digest may contain the fact ([FORGETTING.md](FORGETTING.md)). Routine expiry leaves journals alone.
- A day is written once. If the LLM is down, no journal is written.

Journals are most easily retrieved with `recall_past` and a time range.

## Failure behaviour

| Failure | Result |
|---|---|
| LLM unavailable | whole batch placed by rules; `stats.fallback` counts them |
| LLM returns an invalid store payload | that item falls back to rules |
| Embedder unavailable | row is promoted without a vector; `reconcile_embeddings` fills it in at next daemon start |
| Run raises mid-batch | unprocessed items keep `consolidation_run IS NULL` and are picked up by the next run |

## Tests

`tests/test_v2.py`: `test_consolidation_routes_to_stores_and_validates`, `test_consolidation_without_llm_uses_rules`, `test_journal_written_and_removed_by_forget`, `test_stale_items_are_tagged`, `test_expiry_consolidates_first_then_only_drops_discards`.
