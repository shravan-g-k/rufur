"""Dashboard read models: sessions with their subagent pools, per-pool memories and history, tokens, forgetting."""

UI = {"X-Rufur-Agent": "ui"}
CC = {"X-Rufur-Agent": "claude-code"}


def get(client, path, headers=UI):
    r = client.get(path, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def seed(client):
    sid = client.post("/v1/sessions", json={}, headers=CC).json()["session_id"]
    client.post("/v1/events", json={"content": "I live in Lisbon", "session_id": sid}, headers=CC)
    client.post("/v1/events", json={"content": "I prefer helix for editing", "session_id": sid}, headers=CC)
    client.post(f"/v1/sessions/{sid}/end", headers=CC)
    spawn = client.post("/v1/pools/spawn", json={"purpose": "find the editor", "needs": ["editor"], "session_id": sid},
                        headers=CC).json()  # fmt: skip
    return sid, spawn


def test_sessions_list_their_pools_and_tiers(client):
    sid, spawn = seed(client)
    out = get(client, "/v1/dashboard/sessions")
    s = next(x for x in out["sessions"] if x["id"] == sid)
    assert s["agent"] == "claude-code" and s["events"] == 2
    assert s["stm"].get("active") == 2 and not s["ltm"]
    assert [p["id"] for p in s["pools"]] == [spawn["pool_id"]]
    assert s["pools"][0]["status"] == "open" and s["pools"][0]["seeded_stm"] >= 1
    assert out["ttl"]["base_h"] == 72

    mems = get(client, f"/v1/dashboard/session/{sid}/memories")["items"]
    assert {m["tier"] for m in mems} == {"stm"} and all(m["expires_at"] for m in mems)
    pool = get(client, f"/v1/dashboard/scratch/{spawn['pool_id']}/memories")["items"]
    assert {m["origin"] for m in pool} == {"seeded"}


def test_timeline_replays_promotion_and_forget(client):
    sid, _ = seed(client)
    client.post("/v1/consolidate", json={}, headers=CC)
    mems = get(client, f"/v1/dashboard/session/{sid}/memories")["items"]
    assert {m["tier"] for m in mems} == {"ltm"}
    lisbon = next(m for m in mems if "Lisbon" in m["text"])
    receipt = client.post("/v1/forget", json={"memory_id": lisbon["id"]}, headers=UI).json()["receipt"]

    tl = get(client, f"/v1/dashboard/session/{sid}/timeline")
    kinds = [e["kind"] for e in tl["events"]]
    assert "memory.create" in kinds and "memory.promote" in kinds and "memory.delete" in kinds
    peak_stm = max(p["stm"] for p in tl["series"])
    assert peak_stm == 2 and tl["series"][-1] == {**tl["series"][-1], "stm": 0, "ltm": 1, "forgotten": 1}
    assert get(client, "/v1/dashboard/sessions")["sessions"][0]["forgotten"] == 1

    f = get(client, "/v1/dashboard/forgetting")
    assert f["receipts"][0]["receipt_id"] == receipt["receipt_id"] and f["chain"]["valid"]
    assert all("Lisbon" not in e["content"] for e in f["redacted_events"])
    assert any(f["placeholder"] in e["content"] for e in f["redacted_events"])


def test_pool_close_shows_merged_and_discarded(client):
    sid, spawn = seed(client)
    client.post("/v1/pool/remember", json={"address": spawn["address"], "text": "The user moved to Porto"}, headers=CC)
    client.post("/v1/pool/close", json={"address": spawn["address"]}, headers=CC)
    p = get(client, "/v1/dashboard/sessions")["sessions"][0]["pools"][0]
    assert p["status"] == "closed" and p["merged"] is not None and p["close_reason"] == "closed"
    tl = get(client, f"/v1/dashboard/scratch/{spawn['pool_id']}/timeline")
    assert [e["kind"] for e in tl["events"]][0] == "pool.spawn" and "pool.close" in [e["kind"] for e in tl["events"]]


def test_other_users_sessions_are_hidden(client):
    sid, spawn = seed(client)
    client.post("/v1/agents", json={"name": "stranger"}, headers=UI)
    # A subagent identity is its own user, so it sees no sessions and can't open the parent's.
    sub = {"X-Rufur-Agent": spawn["subagent"]}
    assert get(client, "/v1/dashboard/sessions", headers=sub)["sessions"] == []
    assert client.get(f"/v1/dashboard/session/{sid}/memories", headers=sub).status_code == 404


def test_tokens_are_bucketed(client):
    seed(client)
    client.post("/v1/recall", json={"query": "where does the user live"}, headers=CC)
    out = get(client, "/v1/dashboard/tokens")
    assert out["series"] and out["cumulative"]
    assert out["cumulative"][-1]["full_history"] == 11 and out["cumulative"][-1]["rufur"] > 0  # two short events
    assert any(a["agent"] == "claude-code" for a in out["by_agent"])
    assert len(out["series"]) == 1 and "claude-code" in out["series"][0]["agents"]
    hourly = get(client, "/v1/dashboard/tokens?bucket=hour")["series"]
    assert len(hourly) >= 48 and hourly[0]["recall"] == 0  # quiet hours are filled in as zero


def test_token_sessions_split_after_twenty_quiet_minutes(service):
    from rufur import textutil
    from rufur.dashboard import tokens

    try:
        service.recall("claude-code", "where does the user live")
        textutil.CLOCK_OFFSET = 10 * 60
        service.recall("claude-code", "what editor")
        textutil.CLOCK_OFFSET = 35 * 60
        service.recall("pi", "deadline")
        series = tokens(service, "session")["series"]
        assert [s["agents"] for s in series] == [["claude-code"], ["pi"]]
    finally:
        textutil.CLOCK_OFFSET = 0.0


def test_totals_count_memories_without_a_session(client):
    seed(client)
    client.post("/v1/memories", json={"text": "I prefer vim for editing", "source": "user"}, headers=UI)
    client.post("/v1/consolidate", json={}, headers=CC)
    out = get(client, "/v1/dashboard/sessions")
    assert sum(out["totals"]["memories"]["ltm"].values()) == 3
    assert sum(out["totals"]["unsessioned"]["ltm"].values()) == 1
    assert out["totals"]["pools"]["scratch"] == {"open": 1, "closed": 0}
    items = get(client, "/v1/dashboard/unsessioned/all/memories")["items"]
    assert [m["text"] for m in items] == ["The user's preferred editor is vim."]


def test_raw_catalog_and_rows_are_read_only_and_ui_only(client, service):
    seed(client)
    cat = get(client, "/v1/raw/catalog")
    tables = {t["name"]: t for t in cat["tables"]}
    assert {"memories", "vec_items", "vec_items_chunks", "memories_fts_data", "entities", "edges"} <= set(tables)
    assert tables["vec_items"]["group"] == "vector" and tables["memories_fts_data"]["shadow_of"] == "memories_fts"
    assert tables["memories"]["rows"] == service.db.scalar("SELECT count(*) FROM memories")
    assert {"from_table": "vec_map", "from_column": "rowid", "to_table": "vec_items", "to_column": "rowid", "when": None} in cat["links"]

    mems = get(client, "/v1/raw/tables/memories?limit=1")
    assert mems["columns"][0] == "rowid" and len(mems["rows"]) == 1 and mems["total"] == tables["memories"]["rows"]
    mid = mems["rows"][0][mems["columns"].index("id")]
    src = get(client, f"/v1/raw/tables/memory_sources?column=memory_id&value={mid}")
    assert src["total"] >= 1
    vec = get(client, "/v1/raw/tables/vec_items?limit=1")
    blob = vec["rows"][0][vec["columns"].index("embedding")]
    assert blob["bytes"] == 256 * 4

    assert client.get("/v1/raw/catalog", headers=CC).status_code == 403
    assert client.get("/v1/raw/tables/nope", headers=UI).status_code == 404
    assert client.get('/v1/raw/tables/memories?column=x"; DROP TABLE memories;--&value=1', headers=UI).status_code == 400
    assert vec["columns"].count("rowid") == 1


def test_session_composition_counts_every_memory_once(client):
    sid, _ = seed(client)
    client.post("/v1/consolidate", json={}, headers=CC)
    lisbon = next(m for m in get(client, f"/v1/dashboard/session/{sid}/memories")["items"] if "Lisbon" in m["text"])
    client.post("/v1/forget", json={"memory_id": lisbon["id"]}, headers=UI)

    out = get(client, "/v1/dashboard/sessions")
    s = next(x for x in out["sessions"] if x["id"] == sid)
    # Two memories were created; one is now long-term, one was forgotten (its row is gone, the op isn't).
    assert s["composition"] == {**dict.fromkeys(s["composition"], 0), "ltm": 1, "forgotten": 1}
    assert out["totals"]["storage"]["stores"] and out["totals"]["storage"]["vectors"] >= 1
