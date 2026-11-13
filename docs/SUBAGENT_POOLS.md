# Subagent scratch pools and task handover

Agents delegate. A subagent needs *some* of the user's memory, must not see the rest, and its findings shouldn't vanish when it exits. RUFUR gives each subagent a small, private **scratch pool** and merges the useful part back through the normal write path.

Code: `pools.py`, `service.py` (`pool_*`, `subagent_stopped`), `handover.py`, `integrations/claude-code/hooks/rufur_hook.py` (SubagentStop).

## 1. Lifecycle

```
 parent: pool_spawn(purpose, needs)
    │      seeds the pool BY REFERENCE, creates a scoped identity, returns mem://<pool>/<secret>
    ▼
 parent puts the address in the subagent's prompt
    ▼
 subagent: pool_search / pool_fetch / pool_remember   (can touch only this pool)
    ▼
 pool_close  (parent, subagent, SubagentStop hook, or the sweeper at expiry)
    ├─ useful writes ──▶ parent's STM through WritePath.commit (trust, quarantine, conflicts re-checked)
    └─ the rest discarded; pool and key closed
```

## 2. Spawning (`pools.spawn`)

`pool_spawn(purpose, needs, memory_ids, ttl_minutes, seed_budget)`:

1. **Seed selection within a token budget** (`seed_budget`, default 2000, so the pool is small enough to fit the subagent's context without crowding it): explicit `memory_ids` the parent can read first, then for each `need` (or the purpose) a recall at a halved abstain threshold, adding matches until the budget is spent.
2. **Create the pool** row: `kind='scratch'`, `parent_pool` = the user's STM, `owner_agent`, `expires_at` (default `scratch_ttl_min`, 120 minutes), the purpose, and a `cap_hash`.
3. **Create the capability.** A random secret is generated; only its SHA-256 is stored. The address is `mem://<pool_id>/<secret>`.
4. **Create a subagent identity** `"<parent>/sub-<id>"` with the parent's trust and clearance, and reassign it to user `scratch:<pool_id>`. This identity's ACL grants exactly its pool; it never matches `"*"` and cannot see the user's STM or LTM. A scoped API key is returned.
5. **Seed by reference.** The seeded memories are rows in `pool_members`, not copies. So when the parent later forgets or updates one, the subagent sees the change, and no extra copy of sensitive content exists to clean up.

Subagents cannot spawn further pools.

## 3. The address is a capability

Whoever holds the full address (the subagent, because the parent put it in its prompt, and the parent) may use these tools with it:

| Tool | Behaviour |
|---|---|
| `pool_search(address, query)` | recall restricted to this pool: seeded members plus the subagent's own writes. Uses the parent's clearance. On abstain it hints at `pool_fetch`. |
| `pool_fetch(address, query)` | the subagent needs something the seed missed. The memory layer searches the parent's memory **with the parent's permissions** and adds matches to the pool by reference, within a token budget, audited. The subagent never sees anything it wasn't granted by the layer. |
| `pool_remember(address, text, ...)` | writes into the pool as the subagent identity (source `agent`) |
| `pool_info(address)` | manifest of seeded and own items with token cost |
| `pool_close(address or pool_id, summary?)` | files results back and closes |

This works even where a subagent shares its parent's MCP connection and identity, as in Claude Code, because the capability is passed as a tool *argument*, not inferred from the connection. `resolve` checks the secret with `secrets.compare_digest`, and an unknown or wrong address returns the same error as a nonexistent pool so it can't be probed. A closed or expired pool refuses use.

## 4. Closing and merging back (`pools.close`)

Allowed for the sweeper, the owner agent or the subagent. Steps:

1. If a `summary` is given, it is written into the scratch pool as an `event`.
2. **Decide what is worth keeping** (`_keep`): ask the consolidation placement LLM about the subagent's writes, validate with the same rules as consolidation, and keep everything except `discard` decisions and low-importance (below 0.5) events the model didn't mention. With the LLM down, rules decide.
3. **Merge back through the normal write path.** Each kept memory is re-committed with `WritePath.commit(tier="stm", target_pool=<default LTM pool>)` in the parent's STM, as the subagent identity with its original source type and quotes. So **trust, tombstones, quarantine, instruction-like detection and conflict checks all apply again**. A subagent can't smuggle a planted fact past the gate by writing it into a scratch pool. Created rows get a `from_pool:<id>` flag.
4. Purge everything the pool held, delete its `pool_members`, set `closed_at`, and null the subagent's key hash. The identity row stays so the audit trail still resolves.

The result reports `merged` (each commit's summary) and `discarded`.

## 5. Safety nets

- **Expiry:** the sweeper closes any scratch pool past `expires_at` with reason `expired`.
- **SubagentStop hook:** the Claude Code hook calls `POST /v1/subagents/stopped`, which closes this agent's open pools whose subagent has been quiet for 120 s (`last_seen`).
- **Isolation by identity:** the subagent's user is `scratch:<pool>`, so dashboards and `_user_agents` exclude it from the root user's agent list, while `list_pools` shows scratch pools to their owner.

## 6. Task handover (`handover.py`)

For handing a task from one agent to another (or to a later session):

- `handover_create(task_id, goal, ...)` stores a packet: `title`, `goal`, `steps_done`, `steps_pending`, `decisions` (decision + reason), `open_questions`, `artifacts`, `memory_ids`, `pool`. If no `memory_ids` are given, it recalls memories relevant to the goal and pending steps (a looser relevance bar: gathering context, not answering a question). Only memories the creator can read are kept.
- `handover_resume(task_id)` is permission-aware: ids the resuming agent cannot read are withheld and counted (`withheld_memories`). It returns the packet, each memory with provenance (quotes, role, agent, time), the next step, and a fresh recall for the goal and next step. Status becomes `handed_over` with `to_agent`.
- **Forgetting reaches handovers.** A forget removes the forgotten ids from packets and redacts the text from every field and the title, and verification includes a `handover_packets` text probe ([FORGETTING.md](FORGETTING.md)).

## 7. Tests

`tests/test_v2.py`: `test_subagent_pool_isolation_and_merge_back`, `test_expired_scratch_pool_is_closed_by_sweeper`, `test_memory_address_gives_a_small_searchable_pool_to_root_and_subagent`. `tests/test_core.py`: `test_private_pool_is_isolated`, `test_handover_between_agents`. `tests/test_dashboard.py`: `test_pool_close_shows_merged_and_discarded`.
