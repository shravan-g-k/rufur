"""MCP tools and REST endpoints must expose identical behaviour."""

import json

import anyio
from mcp import Client

from rufur.api.mcp_server import LocalBackend, build_mcp

VOLATILE = {"latency_ms", "created_at", "updated_at", "last_accessed", "access_count"}


def scrub(obj):
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items() if k not in VOLATILE}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


def call(server, tool, args):
    async def go():
        async with Client(server) as c:
            res = await c.call_tool(tool, args)
            assert not res.is_error, res.content
            if res.structured_content is not None:
                sc = res.structured_content
                return sc.get("result", sc) if set(sc) == {"result"} else sc
            return json.loads(res.content[0].text)

    return anyio.run(go)


def test_tools_listed(service):
    server = build_mcp(LocalBackend(service))

    async def go():
        async with Client(server) as c:
            return {t.name for t in (await c.list_tools()).tools}

    names = anyio.run(go)
    assert {"remember", "recall", "inspect", "forget", "confirm_memory", "log_event", "session_start", "session_end",
            "handover_create", "handover_resume", "list_pools"} <= names


def test_mcp_and_rest_parity(service, client):
    server = build_mcp(LocalBackend(service), default_agent="ui")
    wrote = call(server, "remember", {"text": "I prefer helix for editing", "source": "user", "wait": True})
    mid = wrote["results"][0]["memory"]["id"]

    via_mcp = call(server, "recall", {"query": "which editor does the user prefer"})
    via_rest = client.post("/v1/recall", json={"query": "which editor does the user prefer"},
                           headers={"X-Rufur-Agent": "ui"}).json()
    assert scrub(via_mcp) == scrub(via_rest)
    assert via_mcp["items"][0]["id"] == mid

    assert scrub(call(server, "inspect", {"memory_id": mid})) == scrub(
        client.get(f"/v1/memories/{mid}", headers={"X-Rufur-Agent": "ui"}).json()
    )


def test_mcp_session_and_handover_flow(service):
    server = build_mcp(LocalBackend(service), default_agent="claude-code")
    sid = call(server, "session_start", {})["session_id"]
    call(server, "log_event", {"content": "I live in Oslo", "session_id": sid})
    ended = call(server, "session_end", {"session_id": sid})
    assert ended["extraction"]["results"][0]["outcome"] == "created"
    call(server, "handover_create", {"task_id": "t9", "goal": "Book a flight to the user home city", "steps_pending": ["pick dates"]})
    other = build_mcp(LocalBackend(service), default_agent="agent-b")
    resumed = call(other, "handover_resume", {"task_id": "t9"})
    assert resumed["from_agent"] == "claude-code"
    assert any("Oslo" in m["text"] for m in resumed["memories"])
