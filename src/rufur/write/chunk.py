"""Cheap, model-free steps in front of extraction: split text into small chunks, drop chunks that can't
hold a durable memory, and spot sensitive values by pattern.

A weak local model does well with one or two sentences and badly with a page of chat, so the LLM only
ever sees chunks that passed `is_salient`, in batches of a few hundred characters. Every chunk is a verbatim
slice of its event, so it doubles as the claim's supporting quote.
"""

from __future__ import annotations

import re

CHUNK_CHARS = 1200

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[`])|\n+")
_SOFT_BREAK = re.compile(r"(?<=[,;:])\s+")

# What a user says when stating something worth keeping.
_SELF = r"\b(i'm|i am|i've|i have|i live|i work|i moved|i prefer|i like|i love|i hate|i use|i know|i speak|my|mine|we use|we're|we are|our)\b"
_PREFERENCE = r"\b(prefer|always|never|please use|don't use|do not use|i like|i love|i hate|avoid|favou?rite)\b"
_DECISION = r"\b(decided|decision|chose|chosen|going with|we'll use|we will use|instead of|switched to|agreed|settled on|because)\b"
_CHANGE = r"\b(moved|changed to|no longer|update:|actually|slipped|postponed|renamed|switched)\b"
_DATE = r"\b(deadline|due|launch|release|by (monday|tuesday|wednesday|thursday|friday|saturday|sunday|january|february|march|april|may|june|july|august|september|october|november|december)|\d{4}-\d{2}-\d{2})\b|\b(january|february|march|april|may|june|july|august|september|october|november|december) \d{1,2}\b"
_PROCEDURE = r"(\bthen\b.*\bthen\b|\bstep \d|^\s*\d+[.)] |\bfirst\b.*\bthen\b|\bnever deploy\b|\bprocess:)"
_FACTUAL = r"(\b(is|are|was|were|runs|lives|works)\b.*(\d|https?://|\.[a-z]{2,}\b|`[^`]+`))|(\b[A-Z][a-z]+ [A-Z][a-z]+, )|(\d+\s*(requests?|req)?/(min|s|sec|hour)\b)|\b(roster|plan|stack)\b"
_SECRET = r"\b(password|passwd|api[ _-]?key|secret key|access key|pan\b|aadhaar|ssn|credit card|bank account)\b"

SALIENT = re.compile("|".join(f"(?:{p})" for p in (_SELF, _PREFERENCE, _DECISION, _CHANGE, _DATE, _PROCEDURE, _FACTUAL, _SECRET)),
                     re.I | re.S)
# An assistant's reply is mostly explanation ("I changed X because Y"); only an outright decision is worth a model call.
ASSISTANT_SALIENT = re.compile(r"\b(we decided|decided to|we'll use|we will use|going with|settled on|agreed to)\b", re.I)
# Strong enough to keep a chunk that is phrased as a request or question ("can you note that I moved to Pune?").
# Plain "my"/"our" don't count: "can you fix my bug?" is still a request.
_STATEMENT = re.compile(r"\b(i'm|i am|i live|i work|i moved|i prefer|we use|we're using|we decided|decided to|instead of|"
                        r"moved|changed to|no longer|switched)\b", re.I)
_QUESTION = re.compile(r"\?\s*$")
_REQUEST = re.compile(r"^\s*(please\s+)?(can|could|would|will|should|do|does|did|what|why|how|where|when|who|is|are|show|tell|explain|"
                      r"fix|run|write|add|create|make|check|look|find|open|read|list|remove|delete|update the)\b", re.I)

# Sensitivity by pattern: a small model's guess is a hint, these are the floor.
_RESTRICTED = re.compile(
    r"AKIA[0-9A-Z]{16}|\bsk-[A-Za-z0-9_-]{16,}|\bghp_[A-Za-z0-9]{20,}|\b[A-Z]{5}\d{4}[A-Z]\b|\b\d{4}\s\d{4}\s\d{4}\b"
    r"|\b(password|passwd|api[ _-]?key|secret key|access key|private key|aadhaar|ssn|credit card|bank account|iban)\b"
    r"|\b(medication|diagnos\w+|prescri\w+|hypertension|diabetes|diabetic|therapy|therapist|depress\w+|cancer|hiv|salary|lawsuit)\b",
    re.I,
)
_CONFIDENTIAL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|(?<!\d)\+?\d[\d\s-]{8,}\d(?!\d)")
ORDER = ("public", "internal", "confidential", "restricted")


def split_sentences(text: str) -> list[str]:
    return [p.strip() for p in _SENTENCE_END.split(text) if p and p.strip()]


def split_chunks(text: str, max_chars: int = CHUNK_CHARS) -> list[str]:
    """Sentence-bounded pieces of at most `max_chars` (a single unbroken run may exceed it). Verbatim slices."""
    pieces: list[tuple[int, int]] = []
    pos = 0
    for m in list(_SENTENCE_END.finditer(text)) + [None]:
        end = m.start() if m else len(text)
        if text[pos:end].strip():
            pieces.append((pos, end))
        pos = m.end() if m else end
    out: list[str] = []
    cur: tuple[int, int] | None = None
    for start, end in pieces:
        for s, e in _bound(text, start, end, max_chars):
            if cur and e - cur[0] <= max_chars:
                cur = (cur[0], e)
            else:
                if cur:
                    out.append(text[cur[0] : cur[1]].strip())
                cur = (s, e)
    if cur:
        out.append(text[cur[0] : cur[1]].strip())
    return [c for c in out if c]


def _bound(text: str, start: int, end: int, max_chars: int) -> list[tuple[int, int]]:
    """Break one over-long sentence at commas/semicolons, then at spaces."""
    if end - start <= max_chars:
        return [(start, end)]
    spans, pos = [], start
    while end - pos > max_chars:
        window = text[pos : pos + max_chars]
        cut = max((m.end() for m in _SOFT_BREAK.finditer(window)), default=0) or window.rfind(" ") + 1 or max_chars
        spans.append((pos, pos + cut))
        pos += cut
    spans.append((pos, end))
    return spans


def gate_reason(chunk: str, role: str = "user") -> str | None:
    """Why this chunk can't hold a durable memory, or None if it might (a fact, preference, decision, date,
    relation, table row, procedure or secret)."""
    c = chunk.strip()
    if len(c) < 12:
        return "too short to state anything"
    if role == "assistant":
        return None if ASSISTANT_SALIENT.search(c) else "assistant explanation with no decision in it"
    if not SALIENT.search(c):
        return "no fact, preference, decision, date or procedure in it"
    # "Can you run the tests?" / "fix the bug in main.py": a request, not something to remember.
    if (_QUESTION.search(c) or _REQUEST.match(c)) and not _STATEMENT.search(c):
        return "a question or request to the agent, not a statement"
    return None


def is_salient(chunk: str, role: str = "user") -> bool:
    return gate_reason(chunk, role) is None


def salient_chunks(text: str, role: str = "user", max_chars: int = CHUNK_CHARS) -> list[str]:
    return [c for c in split_chunks(text, max_chars) if is_salient(c, role)]


def skip_reason(text: str, role: str = "user", max_chars: int = CHUNK_CHARS) -> str | None:
    """Why no chunk of this message passes the gate, or None if one does."""
    reasons = [gate_reason(c, role) for c in split_chunks(text, max_chars)] or ["empty message"]
    if None in reasons:
        return None
    return max(set(reasons), key=reasons.count)


def detect_sensitivity(text: str) -> str:
    if _RESTRICTED.search(text):
        return "restricted"
    if _CONFIDENTIAL.search(text):
        return "confidential"
    return "internal"


def at_least(a: str, b: str) -> str:
    return a if ORDER.index(a) >= ORDER.index(b) else b
