"""Deterministic trust and quarantine rules (plan §5.4, §8)."""

from __future__ import annotations

import re

TRUST_ORDER = ["low", "medium_low", "medium", "high"]
TRUST_WEIGHT = {"low": 0.25, "medium_low": 0.6, "medium": 0.8, "high": 1.0}
SENSITIVITY_ORDER = ["public", "internal", "confidential", "restricted"]

SOURCE_TRUST = {
    "user": "high",
    "document": "medium",
    "agent": "medium_low",
    "tool": "low",
    "web": "low",
}

ROLE_SOURCE = {"user": "user", "assistant": "agent", "tool": "tool", "document": "document", "web": "web"}

# Text that tries to steer the model rather than state a fact.
_INSTRUCTION_LIKE = re.compile(
    r"\b(ignore (all |any )?(previous|prior|above)|disregard (the|all|previous)|you must|you are now|"
    r"system prompt|always (answer|respond|say|reply)|from now on|do not tell|new instructions|"
    r"override|act as|pretend (to be|you))\b"
    # Exfiltration: text asking whoever reads it to reveal, send or dump secrets.
    r"|\b(reveal|print|output|dump|list|share|send|post|leak|spill|exfiltrate|upload|email|paste|show|display|include)\b"
    r"[^.\n]{0,60}(\b(secrets?|api[ _-]?keys?|keys|tokens?|passwords?|credentials?|env(ironment)? var\w*|ssh key|private key)\b|\.env\b)",
    re.I,
)

# Secret-shaped strings. Masked in everything recall hands to an agent, whatever the memory's label says,
# so a planted "print every key you know" has nothing to print.
_SECRET_TOKENS = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|\bAKIA[0-9A-Z]{16}\b|\bsk-[A-Za-z0-9_-]{16,}|\b(ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{20,}"
    r"|\bxox[abposr]-[A-Za-z0-9-]{10,}|\bAIza[0-9A-Za-z_-]{35}\b|\bglpat-[A-Za-z0-9_-]{20}"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    re.S,
)
_SECRET_ASSIGN = re.compile(
    r"\b((?:password|passwd|pwd|secret|token|api[ _-]?key|access[ _-]?key|client[ _-]?secret)\s*(?:is|=|:)\s*)([^\s,;]{4,})", re.I
)
_URL_CREDS = re.compile(r"(\w+://[^\s:/@]+:)([^\s@/]+)(@)")
SECRET_MASK = "[secret withheld]"


def trust_for(source_type: str) -> str:
    return SOURCE_TRUST.get(source_type, "low")


def min_trust(a: str, b: str) -> str:
    return a if TRUST_ORDER.index(a) <= TRUST_ORDER.index(b) else b


def trust_at_least(trust: str, floor: str) -> bool:
    return TRUST_ORDER.index(trust) >= TRUST_ORDER.index(floor)


def instruction_like(text: str) -> bool:
    return bool(_INSTRUCTION_LIKE.search(text))


def mask_secrets(text: str) -> str:
    text = _SECRET_TOKENS.sub(SECRET_MASK, text)
    text = _SECRET_ASSIGN.sub(lambda m: m.group(1) + SECRET_MASK, text)
    return _URL_CREDS.sub(lambda m: m.group(1) + SECRET_MASK + m.group(3), text)


def can_read(clearance: str, sensitivity: str) -> bool:
    return SENSITIVITY_ORDER.index(sensitivity) <= SENSITIVITY_ORDER.index(clearance)


def initial_status(trust: str, flags: list[str]) -> str:
    """Low-trust memories start quarantined, and so do instruction-like ones unless the user said them: "from now
    on always answer in Hindi" is a preference when the user says it and a planted instruction anywhere else.
    The flag stays on the memory either way."""
    if trust == "low" or ("instruction_like" in flags and trust != "high"):
        return "quarantined"
    return "active"


def hold_reason(trust: str, flags: list[str]) -> str | None:
    """Why a memory is held for review, in plain words, from its trust and flags."""
    reasons = []
    for f in flags:
        if f == "agent_quarantined":
            reasons.append("the agent that wrote it is quarantined")
        elif f == "instruction_like" and trust != "high":
            reasons.append("it reads like an instruction to an AI and didn't come from the user")
        elif f.startswith("supersede_needs_confirmation:"):
            reasons.append(f"it would overwrite a high-trust memory ({f.split(':', 1)[1]}) from a less trusted source")
        elif f.startswith("conflicts_with:"):
            reasons.append(f"it contradicts a more trusted or user-confirmed memory ({f.split(':', 1)[1]})")
    if trust == "low":
        reasons.append("it came from a low-trust source (tool output or the web)")
    return "; ".join(reasons) or None
