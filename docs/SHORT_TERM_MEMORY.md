# Short-term memory (STM)

STM is where every new memory lands first. It is a **per-user, shared** scratch tier with a sliding lifetime: things you said in the last few days are instantly available to every agent you use, and anything not worth keeping falls away on its own.

Code: `src/rufur/stm.py` (lifecycle), `src/rufur/write/pipeline.py` (placement), `src/rufur/scheduler.py` (when it runs).

## What it is, concretely

STM is not a separate table. It is `memories` rows with `tier = 'stm'` that live in a per-user pool named `stm:<user_id>` (`MemoryService.stm_pool`). The pool's ACL is `{"read": ["user:<id>"], "write": ["user:<id>"]}`, so every agent of that user can read and write it. This is why a fact told to Claude Code is visible to opencode a moment later.

Each STM row also carries:

| Column | Role in STM |
|---|---|
| `expires_at` | When it will be swept. Set to `created_at + stm_base_ttl_h` on insert. |
| `target_pool` | The long-term pool the item will be promoted into. Decides visibility (below). |
| `consolidation_run` | `NULL` until consolidation has looked at it. |
| `store` | `discard` if consolidation decided it is not worth keeping long term. |

## How items get in

1. **Capture.** Hooks and `log_event` store every message as an `events` row.
2. **Extraction** turns salient chunks into claims (see the write path in the [README](../README.md#5-the-write-path)). It runs at a checkpoint (6 salient events pending), after 30 s of silence, at session end, or when the sweeper finds an abandoned session.
3. **Commit.** `WritePath.commit(..., tier="stm")` runs trust, tombstone and conflict checks and inserts the row with `expires_at = now + 72 h`.
4. **Explicit `remember`** writes straight into STM too. `long_term=true` additionally consolidates the new item immediately.

Trust, quarantine and conflict handling apply in STM exactly as they do in LTM: a planted fact is held here, not later.

## Visibility: STM obeys the destination's privacy

STM is shared by all of a user's agents, but memories may be headed for private long-term pools. `Filter.sql` therefore treats an STM row as visible only if `COALESCE(target_pool, default_pool)` is a pool the reader can see. A private pool's memories stay private even while they sit in the shared STM.

## Lifetime: a sliding window with a hard cap

Config (`config.py`): `stm_base_ttl_h = 72`, `stm_extend_h = 24`, `stm_max_ttl_h = 168`, `stm_grace_h = 24`.

- **Base:** an item expires 72 h after creation.
- **Use extends it.** Every recall that returns an STM item calls `stm.extend_expiry`, which sets

  ```
  expires_at = MIN(created_at + 168h, MAX(created_at + 72h, now + 24h))
  ```

  So a use keeps the item alive at least 24 h from that use, never shorter than the base, and never past 7 days from creation.
- **Pinned items** are never archived by the sweeper.

Recall also records the *age* of each STM item when it is reused (`stm_ages_h` in the `metrics` row), which feeds TTL tuning.

## What happens at expiry: the sweeper

`stm.sweep` runs every `sweep_every_min` (15) minutes from the scheduler, and by hand with `rufur sweep` or `POST /v1/maintenance/sweep`. Expiry never silently drops something nobody reviewed. Steps, in order:

| Step | Action |
|---|---|
| 0 | Sessions that went quiet for `session_idle_extract_min` (10 min) with unprocessed events get extracted (clients often just quit without ending a session). |
| 1 | Expiring items **nobody has consolidated** (`consolidation_run IS NULL`, status active or disputed) are sent through consolidation first, with trigger `expiry`. If the LLM is down, rules do the filing. |
| 2 | **Archive** what consolidation decided to discard (`store='discard'`) and anything still `quarantined` that nobody reviewed in time. Archived rows are hidden but recoverable. |
| 3 | **Hard-delete** archived STM items after the `stm_grace_h` (24 h) grace period. This is routine expiry, not a user forget, so no tombstone is written and the fact can be learned again. |
| 4 | Close scratch pools whose subagent never closed them (`pools.expires_at` passed). |
| 5 | Drop raw `events` older than `stm_max_ttl_h + stm_grace_h` that no memory cites any more. |

After a purge or event drop the database is compacted. The sweep also records `last_sweep` in `meta`, which the scheduler uses to pace itself.

## Related maintenance in the same sweep

`MemoryService.sweep` wraps `stm.sweep` and adds two things:

- `resolve.apply_defaults`: open conflicts older than 7 days get the default rule (see [CONFLICT_RESOLUTION.md](CONFLICT_RESOLUTION.md)).
- `receipts.recheck`: forgotten content that came back by a path that skipped the write-time tombstone check is deleted again (see [FORGETTING.md](FORGETTING.md)).

## Triggers that move STM into LTM

STM is emptied by consolidation, not by time alone. Consolidation runs when:

- an agent calls `consolidate` (MCP/REST, after finishing a task),
- nightly, after 03:00 local time,
- on daemon start if the last run is more than 20 h old (the laptop may have been asleep),
- STM grows large: more than 200 unconsolidated items, or more than 300 events since the last run (`_maybe_size_trigger`),
- an item is about to expire unconsolidated (sweeper step 1).

See [LONG_TERM_MEMORY.md](LONG_TERM_MEMORY.md).

## Tuning the lifetime

`rufur tune-stm` (`stm.suggest_ttl`) reads the `stm_ages_h` samples recorded by recall and reports the median and 95th percentile age at which STM items are actually reused. It suggests `stm_base_ttl_h` = that p95, clamped to **48-96 h**: shorter loses context between sessions, longer is what LTM is for. It needs at least 20 reuses before suggesting anything. `GET /v1/stm/stats` also reports item counts by status and how many are unconsolidated.

## Tests

`tests/test_v2.py`: `test_stm_is_shared_across_a_users_agents`, `test_stm_expiry_slides_with_use_and_is_capped`, `test_expiry_consolidates_first_then_only_drops_discards`, `test_tune_stm_suggests_ttl_from_reuse_age`. Expiry is tested by moving `textutil.CLOCK_OFFSET` forward.
