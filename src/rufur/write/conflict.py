"""Update / contradiction engine (plan §6).

Candidates share the new claim's normalised (subject, predicate) key, or are
close vector neighbours in the same pool. Exact duplicates are decided by rule;
everything else goes to the classify-role LLM. Any doubt — including an LLM
that is down or returns junk — resolves to "unclear" for same-key pairs.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from ..db.conn import Database, pack_vector
from ..llm import LLM, LLMUnavailable
from ..textutil import norm_key

Outcome = Literal["duplicate", "update", "context", "coexist", "unclear", "unrelated"]

LIVE_STATUSES = ("active", "disputed", "quarantined")

# An "update" must be backed by language that signals change in the source itself;
# otherwise two different values for one key are a contradiction, not an update.
CHANGE_MARKERS = re.compile(
    r"\b(now|moved|moving|chang(e|ed|ing)|switch(ed|ing)?|updat(e|ed|ing)|actually|no longer|not anymore|"
    r"instead|correct(ion|ed)?|postponed|extended|rescheduled|pushed (back|out)|delayed|brought forward|"
    r"replac(e|ed|ing)|from now on|these days|recently|nowadays|new)\b",
    re.I,
)


# Attributes that naturally hold several values at once: a new value adds to the set, it doesn't replace it.
MULTI_VALUED = re.compile(
    r"\b(goals?|interests?|skills?|hobb(y|ies)|languages?|technolog(y|ies)|tools?|frameworks?|projects?|friends?|"
    r"allerg(y|ies)|likes?|dislikes?|favou?rites?|contacts?|members?|features?|stack|courses?|certifications?)\b",
    re.I,
)


def multi_valued(predicate: str) -> bool:
    return bool(MULTI_VALUED.search(predicate or ""))


class Verdict(BaseModel):
    # Required in the schema the model is constrained to, so it must answer same_attribute; optional when parsing.
    model_config = {"json_schema_extra": {"required": ["outcome", "same_attribute"]}}

    outcome: Outcome
    # Do both claims answer the same question? Only then can they contradict. None = the model didn't say.
    same_attribute: bool | None = None
    scope_old: str | None = None
    scope_new: str | None = None
    reason: str = ""


SYSTEM = """You compare an EXISTING memory with a NEW claim about the same thing and decide how they relate.

- duplicate: they say the same thing.
- update: the NEW claim replaces the EXISTING one (the fact changed over time, or the user corrected it).
  Choose this only when the new text clearly signals change or correction ("now", "moved", "changed to",
  "actually", "no longer", a newer date).
- context: both can be true at once because they apply to different situations (e.g. work vs. personal,
  different projects). Give a short scope for each.
- coexist: both are true at the same time: another value of a list-like attribute (a second goal, more
  skills, another interest), or a different aspect of the same thing (a project's status vs. its purpose).
- unclear: they answer the SAME question with incompatible answers and you cannot tell which is right or
  whether it changed.
- unrelated: they are about different things.

Also set `same_attribute`: true only if both claims answer the same question about the same subject
(e.g. "where does the user live?"). Two facts about one project that answer different questions are false.

When unsure between update and unclear, choose unclear. When unsure between unclear and coexist, ask
whether both could be true at once; if yes, choose coexist."""


class SameAttribute(BaseModel):
    same: bool


ATTRIBUTE_SYSTEM = """Two facts are stored as subject / attribute / value. Do the two attributes ask the SAME
question, so that the two values could contradict each other? "home city" and "location": true.
"description" and "status": false. "deadline" and "due date": true. "goal" and "skill": false.
Return {"same": true} or {"same": false}."""


def _same_attribute(llm: LLM, existing: dict, new_subject: str, new_predicate: str, new_value: str) -> tuple[bool | None, int]:
    prompt = (f"A: {existing.get('subject')} / {existing.get('predicate')} / {existing.get('value')}\n"
              f"B: {new_subject} / {new_predicate} / {new_value}")  # fmt: skip
    try:
        out, usage = llm.complete("classify", ATTRIBUTE_SYSTEM, prompt, SameAttribute)
        return out.same, usage.tokens_in + usage.tokens_out
    except LLMUnavailable:
        return None, 0


def find_candidates(db: Database, pool_ids: list[str], subject: str, predicate: str,
                    vec: list[float] | None, exclude_id: str | None = None) -> list[tuple[dict, str]]:  # fmt: skip
    """Returns [(memory, how)] where how is 'key' or 'vector'."""
    marks = ",".join("?" * len(pool_ids))
    status_marks = ",".join("?" * len(LIVE_STATUSES))
    found: dict[str, tuple[dict, str]] = {}
    if subject and predicate:
        for m in db.all(
            f"SELECT * FROM memories WHERE pool_id IN ({marks}) AND subject = ? AND predicate = ? AND status IN ({status_marks})",
            (*pool_ids, norm_key(subject), norm_key(predicate), *LIVE_STATUSES),
        ):
            found[m["id"]] = (m, "key")
    if vec is not None:
        rows = db.all(
            f"""WITH knn AS (SELECT rowid, distance FROM vec_items WHERE embedding MATCH ? AND k = 20)
                SELECT m.*, knn.distance FROM knn
                JOIN vec_map vm ON vm.rowid = knn.rowid
                JOIN memories m ON m.id = vm.memory_id
                WHERE vm.kind = 'memory' AND m.pool_id IN ({marks}) AND m.status IN ({status_marks})
                ORDER BY knn.distance""",
            (pack_vector(vec), *pool_ids, *LIVE_STATUSES),
        )
        for m in rows:
            # cosine distance; 0.15 ≈ similarity 0.85
            if m.pop("distance") <= 0.15 and m["id"] not in found:
                found[m["id"]] = (m, "vector")
            if len(found) >= 5:
                break
    found.pop(exclude_id or "", None)
    return list(found.values())


def classify(llm: LLM, existing: dict, new_text: str, how: str, new_value: str, evidence: str = "",
             new_subject: str = "", new_predicate: str = "", source_quote: str = "") -> tuple[Verdict, int]:  # fmt: skip
    """Returns (verdict, llm tokens used). `evidence` is the raw source text behind the new claim."""
    if how == "key" and norm_key(existing["value"]) == norm_key(new_value):
        return Verdict(outcome="duplicate", reason="same key and value"), 0
    if norm_key(existing["text"]) == norm_key(new_text):
        return Verdict(outcome="duplicate", reason="same text"), 0
    prompt = f"EXISTING ({existing['source_type']}, trust {existing['trust']}): {existing['text']}\nNEW: {new_text}"
    source = source_quote or evidence[:500]
    if source and norm_key(source) != norm_key(new_text):
        # The claim's wording is built from its fields and drops cues like "moved" or "actually"; the source keeps them.
        prompt += f"\nNEW SOURCE: {source}"
    try:
        verdict, usage = llm.complete("classify", SYSTEM, prompt, Verdict)
        tokens = usage.tokens_in + usage.tokens_out
    except LLMUnavailable:
        verdict, tokens = Verdict(outcome="unclear" if how == "key" else "unrelated", reason="classifier unavailable"), 0
    if verdict.outcome == "update" and not CHANGE_MARKERS.search(evidence or new_text):
        verdict = Verdict(outcome="unclear", same_attribute=verdict.same_attribute,
                          reason=f"no change signal in the source; classifier said update: {verdict.reason}")  # fmt: skip
    if (how == "vector" and verdict.outcome == "unclear" and existing.get("predicate") and new_predicate
            and norm_key(new_predicate) != existing["predicate"]):  # fmt: skip
        # Whole-sentence judgements from small models over-call conflicts between different aspects of one
        # thing; a narrow "same attribute?" question about the two keys is far more reliable.
        same, extra = _same_attribute(llm, existing, new_subject, new_predicate, new_value)
        tokens += extra
        if same is not None:
            verdict.same_attribute = same
    if how == "key" and multi_valued(existing["predicate"]) and verdict.outcome in ("unclear", "unrelated"):
        # "goal: internship" and "goal: open source" are both true; a list-like attribute only grows.
        verdict = Verdict(outcome="coexist", reason=f"multi-valued attribute; classifier said {verdict.outcome}")
    elif how == "key" and verdict.outcome == "unrelated":
        # Same subject and predicate can't be unrelated; treat the disagreement as unresolved.
        verdict = Verdict(outcome="unclear", reason=f"same key, classifier said unrelated: {verdict.reason}")
    elif how == "vector" and verdict.outcome == "unclear" and not (
        verdict.same_attribute
        # Small models often omit the flag; then an "unclear" between claims about one subject stands.
        or (verdict.same_attribute is None and new_subject and norm_key(new_subject) == existing.get("subject"))
    ):
        # A near neighbour is only a contradiction if it answers the same question. Similar wording about
        # different aspects ("in development" vs "a recruitment platform") is not a dispute.
        verdict = Verdict(outcome="coexist", reason=f"different attribute; classifier said unclear: {verdict.reason}")
    return verdict, tokens
