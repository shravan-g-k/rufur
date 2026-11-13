# Conflict resolution: updates, contradictions and decisions

Memories change. A user moves city, a deadline slips, two agents learn different things. RUFUR separates three situations and handles each differently:

| Situation | Example | Outcome |
|---|---|---|
| **Duplicate** | "I live in Pune" said twice | corroborate: attach the new source, no new row |
| **Update** | "I moved to Mumbai" | new version replaces the old one; the old one is kept as history |
| **Disagreement** | "deadline is Dec 15" vs "deadline is Dec 20", no sign of change | a *conflict*: settled by rules, then the memory layer's LLM, then the calling agent, then a default |

There is no UI step. The conflict is handed to whichever agent is talking to the user.

Code: `write/conflict.py` (detect and classify), `write/pipeline.py` (act on the verdict), `write/resolve.py` (settle), `service.py` (`pending_decisions`, `resolve_conflict`, `auto_review`).

## 1. Finding candidates

`conflict.find_candidates` looks at live memories (`active`, `disputed`, `quarantined`) in the user's STM and the target LTM pool:

- **`key`**: same normalised `(subject, predicate)`.
- **`vector`**: nearest neighbours with cosine distance <= 0.15 (similarity about 0.85), at most 5 candidates overall.

Subjects that are the user's own name are rewritten to `user` first, so "Rahul's city" and "the user's city" share a key.

## 2. Classifying a candidate

`conflict.classify` returns a `Verdict`:

| Outcome | Meaning |
|---|---|
| `duplicate` | same fact |
| `update` | the new claim replaces the old (the fact changed or the user corrected it) |
| `context` | both true, in different situations (work vs. personal); each gets a `scope` |
| `coexist` | both true at once (another value of a list-like attribute, or a different aspect) |
| `unclear` | same question, incompatible answers, can't tell which is right |
| `unrelated` | different things |

Rules run before and after the LLM (the `classify` role) so a small model cannot cause damage:

- **Duplicates by rule:** same key and same normalised value, or identical normalised text, never reach the LLM.
- **The LLM sees the source, not just the claim.** The claim text is rebuilt from fields and loses words like "moved" or "actually", so the prompt also includes `NEW SOURCE:` the verbatim quote.
- **"Update" needs a change signal.** If the LLM says `update` but neither the source text nor the claim contains a change marker (`now`, `moved`, `changed`, `switched`, `actually`, `no longer`, `postponed`, `rescheduled`, `instead`, `new`...), it is downgraded to `unclear`. Two values with no sign of change are a contradiction, not an update.
- **Multi-valued attributes only grow.** If the predicate looks list-like (goals, skills, interests, languages, tools, projects, allergies, favourites, members...) then `unclear` or `unrelated` becomes `coexist`. Forgetting one goal must not wipe the others either.
- **Same key can't be "unrelated".** That becomes `unclear`.
- **Vector neighbours are not conflicts unless they answer the same question.** For a vector-only `unclear`, a narrow second LLM question asks whether the two *attributes* are the same ("home city" vs "location": yes; "description" vs "status": no). If not, the verdict is `coexist`. Small models over-call conflicts between different aspects of one thing; the narrow question is far more reliable.
- **LLM down:** `unclear` for key matches, `unrelated` for vector matches. Doubt always resolves conservatively.

## 3. Acting on the verdict (`WritePath.commit`)

| Verdict | Action |
|---|---|
| `duplicate` | `_corroborate`: attach the source, `confidence += 0.1`, and `corroboration += 1` only if the evidence is **independent** (a different agent or source type, not the same agent repeating itself). May lift quarantine, see [TRUST_AND_QUARANTINE.md](TRUST_AND_QUARANTINE.md). |
| `update` | the old row becomes `superseded` with `valid_to = now` and `superseded_by = <new id>`. **Exception:** if the old fact is `high` trust and the new source isn't, the newcomer is held (`quarantined`, flag `supersede_needs_confirmation:<id>`) and a conflict opens. |
| `context` | both stay active; scopes are stored (`scope` column) and shown in recall. |
| `coexist` | both stay active; recorded in `related`. |
| `unclear` | settle by rules first: if the existing memory is user-confirmed or more trusted, the newcomer is held (`conflicts_with:<id>`); if the newcomer is more trusted, it supersedes; if trust is equal, both become `disputed`. |

After insertion, a `conflicts` row is opened (`resolve.open_conflict`) when a disagreement was not settled by those rules:

- **`override`**: a less-trusted (or quarantined) newcomer vs. a trusted memory.
- **`dispute`**: equal trust, both live and `disputed`.

## 4. The resolver: four steps, in order

`resolve.auto_resolve` runs immediately after the write (`service._settle`). It is also re-run on demand by `auto_review`. Each step is recorded in `conflicts.attempts`, so the UI shows *why* something is still open.

### Step 1: rules (`_rules`)

- If the newcomer is `low` trust (tool or web output), it **cannot** replace an existing memory without corroboration: the most trusted existing memory wins.
- If exactly one side is `user_confirmed`, it wins.
- For a `dispute`, a side with at least 2 more independent corroborations than the other wins.

### Step 2: the memory layer's LLM (`_judge`, role `resolve`)

The model re-reads both claims **with their full source messages, who said them, the source type and the date**, and answers one of:

| Decision | Meaning |
|---|---|
| `A` / `B` | A (existing) or B (newcomer) is correct now |
| `same` | not a real disagreement; keep the more complete wording ("December 15" vs "December 15, 2026") |
| `both` | both true in different contexts; needs a scope for each |
| `unknown` | the evidence doesn't settle it; `ask_user` says whether only the user could know |

Its verdict is applied only if **all** of these hold:

- confidence is at least `auto_resolve_confidence` (0.8);
- for `A`/`B`, it **never hands the win to a less trusted source** than what it replaces (the LLM may confirm the more trusted side, not overturn it);
- for `same`, nothing is lost either way, so a less trusted wording still yields to the more trusted one;
- for `both`, both scopes are present.

If the model says `unknown` with `ask_user=true` (for example two different names for the user and no correction in either message), the conflict gets `ask_user=1`: both memories stay disputed, every recall touching them asks for clarification, and it is **never** settled by the default in step 4.

### Step 3: the calling agent

Anything still open is handed to the agent that is talking to the user, in two places:

- **`recall` results** carry `decisions_needed` for open conflicts touching the returned memories, and the rendered context gets an appended block. Session bootstrap includes up to 3 pending decisions.
- **`pending_decisions`** (MCP) / `GET /v1/decisions` lists them all.

Each decision has the question, the options (text, source, trust, agent, time, quote, corroboration), a `recommendation`, `needs_user`, `how_to_answer`, what was `tried`, and a `default_at` date. The agent answers with `resolve_conflict(conflict_id, action, ...)`:

- `keep` + `winner=<memory_id>`: the winner is activated; losers become `superseded` (kept in history) or, if they were held and never accepted, `rejected`;
- `both` + `scopes`: all stay active with scopes;
- `neither`: all rejected.

**When must the user decide?** `needs_user` is true when a less-trusted source would replace something the user said (`needs_user(mems)`), or when `ask_user` is set. In that case:

- `resolve_conflict` raises `AccessDenied` for a non-user decision that would swap in the less trusted source (the planted-fact attack), except for choosing the trusted memory;
- `decided_by="user"` is **believed only if the user actually wrote to this agent recently** (`_user_present`: a captured user turn within `user_presence_window_min`, 30 min; the UI is always present). Otherwise it is treated as the agent's own decision;
- a user decision also upgrades the winner to `trust=high` with the `user_confirmed` flag, which makes it win future rule-based conflicts.

`confirm_memory` is the one-memory shortcut: confirming or rejecting a memory that is part of open conflicts resolves them in its favour or against it.

### Step 4: the default, after 7 days

Nothing waits forever. `resolve.apply_defaults` (run from the sweeper) settles open conflicts older than `decision_default_after_days` (7) by `_default_choice`: higher trust, then user-confirmed, then more corroboration, then the newer statement. The loser is **kept in history**, not deleted. `ask_user` conflicts are exempt.

## 5. Conflicts across time and agents

Two statements may never overlap in a single write. The consolidation sweep (see [LONG_TERM_MEMORY.md](LONG_TERM_MEMORY.md)) compares promoted items against older LTM with the same key and a different value and marks both `disputed`. Disputed memories are still returned (with `disputed="true"` and a 0.9 score multiplier) so the agent sees both sides rather than a silently chosen one.

## 6. Status transitions

```
        ┌──────── duplicate / corroborated ─────────┐
 new ──▶ active ──── update (change signal) ──▶ superseded (valid_to set, history kept)
   │       │ ▲
   │       │ └── resolver / user keeps it
   │       └──── unclear, equal trust ──▶ disputed ──▶ resolved: active / superseded / rejected
   └── low trust, instruction-like, or challenges a trusted fact ──▶ quarantined ──▶ active | rejected
```

Every transition writes an `ops` row (supersede, dispute, quarantine, confirm, reject, resolve) and an `audit` row, and publishes an event on the SSE stream.

## 7. Tests

`tests/test_conflicts.py` covers the resolver paths. In `tests/test_core.py`: `test_update_supersedes_and_keeps_history`, `test_unclear_conflict_marks_both_disputed`, `test_low_trust_cannot_overwrite_high_trust`, `test_update_without_change_signal_becomes_dispute`. In `tests/test_v2.py`: `test_unclear_conflicts_resolved_by_trust`, `test_independent_corroboration_counts`.
