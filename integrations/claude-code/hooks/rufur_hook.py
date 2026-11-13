#!/usr/bin/env python3
"""Claude Code ↔ RUFUR hook (stdlib only). Every event fails open: memory must never block Claude.

SessionStart       start a RUFUR session, preload the working set, inject it as context
UserPromptSubmit   capture the prompt; inject relevant memories not already sent this session
PostToolUse        capture WebFetch/WebSearch output (low trust)
Stop               capture the assistant's final reply
SubagentStop       close this agent's idle subagent pools so their findings come back
SessionEnd         end the RUFUR session (extraction runs in the daemon's background)

Env: RUFUR_URL (default http://127.0.0.1:7437), RUFUR_API_KEY or RUFUR_AGENT (default claude-code),
RUFUR_CAPTURE_TOOLS (default "WebFetch,WebSearch"), RUFUR_POOL, RUFUR_PROMPT_BUDGET (default 300).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

URL = os.environ.get("RUFUR_URL", "http://127.0.0.1:7437").rstrip("/")
STATE = Path(os.environ.get("RUFUR_HOME", Path.home() / ".rufur")) / "hook_sessions.json"
CAPTURE_TOOLS = set(filter(None, os.environ.get("RUFUR_CAPTURE_TOOLS", "WebFetch,WebSearch").split(",")))
PROMPT_BUDGET = int(os.environ.get("RUFUR_PROMPT_BUDGET", "300"))
MAX_CHARS = 4000


def _request(path: str, body: dict | None = None, timeout: float = 3.0) -> dict:
    headers = {"Content-Type": "application/json"}
    if key := os.environ.get("RUFUR_API_KEY"):
        headers["Authorization"] = f"Bearer {key}"
    else:
        headers["X-Rufur-Agent"] = os.environ.get("RUFUR_AGENT", "claude-code")
    req = urllib.request.Request(URL + path, data=json.dumps(body or {}).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _sessions() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))


def _inject(event: str, context: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}))


def _session(cc_session: str, cwd: str | None = None, prompt: str | None = None) -> tuple[str, str | None]:
    """RUFUR session for this Claude Code session; bootstraps one (with a working set) if needed."""
    state = _sessions()
    if cc_session in state:
        return state[cc_session], None
    body = {"project": Path(cwd).name if cwd else None, "prompt": prompt}
    if pool := os.environ.get("RUFUR_POOL"):
        body["pool"] = pool
    out = _request("/v1/bootstrap", body, timeout=6)
    state[cc_session] = out["session_id"]
    _save(state)
    return out["session_id"], out.get("context") or ""


def _last_assistant_text(transcript_path: str) -> str | None:
    try:
        lines = Path(transcript_path).read_text().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = entry.get("message") or {}
        if entry.get("type") == "assistant" and msg.get("role") == "assistant":
            parts = [c.get("text", "") for c in msg.get("content", []) if isinstance(c, dict) and c.get("type") == "text"]
            if text := "\n".join(p for p in parts if p).strip():
                return text
    return None


SESSION_NOTE = (
    "RUFUR memory is active (session {sid}). Relevant memories are added to your context automatically. "
    "Required at the end of EVERY turn, whatever its size: call the rufur MCP tool remember with the user's message "
    "word for word (source=\"user\"), and again with what you did or decided (source=\"agent\") if anything. Do it "
    "silently; duplicates are merged. Use recall or recall_past to look further back. Before delegating to a subagent, "
    "call pool_spawn with session_id=\"{sid}\" and put the returned memory address in its prompt."
)


def main() -> None:
    data = json.load(sys.stdin)
    event = data.get("hook_event_name")
    cc_session = data.get("session_id", "default")

    if event == "SessionStart":
        state = _sessions()
        if data.get("source") in ("resume", "compact") and cc_session in state:
            # Context was rebuilt; reload the working set into the same RUFUR session.
            sid = state[cc_session]
            context = _request("/v1/bootstrap", {"session_id": sid, "project": Path(data.get("cwd", "")).name}, 6).get("context")
        else:
            state.pop(cc_session, None)
            _save(state)
            sid, context = _session(cc_session, data.get("cwd"))
        _inject("SessionStart", "\n\n".join(filter(None, [SESSION_NOTE.format(sid=sid), context])))

    elif event == "UserPromptSubmit" and (prompt := data.get("prompt")):
        sid, _ = _session(cc_session, data.get("cwd"), prompt)
        _request("/v1/events", {"content": prompt[:MAX_CHARS], "role": "user", "session_id": sid})
        out = _request("/v1/recall", {"query": prompt[:1000], "session_id": sid, "token_budget": PROMPT_BUDGET,
                                      "verify": False, "exclude_sent": True}, timeout=2.5)  # fmt: skip
        if not out.get("abstained") and out.get("items"):
            _inject("UserPromptSubmit", out["context"])

    elif event == "PostToolUse" and data.get("tool_name") in CAPTURE_TOOLS:
        sid, _ = _session(cc_session, data.get("cwd"))
        result = data.get("tool_response")
        text = result if isinstance(result, str) else json.dumps(result)[:MAX_CHARS]
        role = "web" if data["tool_name"].startswith("Web") else "tool"
        _request("/v1/events", {"content": f"[{data['tool_name']}] {text}"[:MAX_CHARS], "role": role, "session_id": sid})

    elif event == "Stop" and data.get("transcript_path"):
        if text := _last_assistant_text(data["transcript_path"]):
            sid, _ = _session(cc_session, data.get("cwd"))
            _request("/v1/events", {"content": text[:MAX_CHARS], "role": "assistant", "session_id": sid})

    elif event == "SubagentStop":
        sid = _sessions().get(cc_session)
        _request("/v1/subagents/stopped", {"session_id": sid}, timeout=8)

    elif event == "SessionEnd":
        state = _sessions()
        if sid := state.pop(cc_session, None):
            _request(f"/v1/sessions/{sid}/end?background=true")
            _save(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the user's Claude Code session over memory
        print(f"rufur hook: {e}", file=sys.stderr)
    sys.exit(0)
