"""Short-term memory, consolidation, working set, subagent pools and the v2 safety rules."""

import json
import re

import pytest

from rufur import textutil
from rufur.embed import HashEmbedder
from rufur.llm import FakeLLM
from rufur.service import AccessDenied, MemoryService

from .conftest import fake_handler

HOUR = 3600


@pytest.fixture(autouse=True)
def reset_clock():
    textutil.CLOCK_OFFSET = 0.0
    yield
    textutil.CLOCK_OFFSET = 0.0


def advance(hours: float) -> None:
    textutil.CLOCK_OFFSET += hours * HOUR


def make(cfg, consolidate_answer=None, extra=None):
    """Service whose fake LLM also answers the consolidate role (`consolidate_answer(ids_and_texts) -> decisions`)."""

    def handler(role, prompt):
        if role == "consolidate" and "MEMORIES:" in prompt:
            if consolidate_answer is None:
                return None
            items = re.findall(r'<memory id="([^"]+)"[^>]*>(.*?)</memory>', prompt.split("MEMORIES:", 1)[1])
            return {"decisions": consolidate_answer(items)}
        if extra and (out := extra(role, prompt)) is not None:
            return out
        return fake_handler(role, prompt)

    return MemoryService(cfg, llm=FakeLLM(cfg, handler), embedder=HashEmbedder(cfg.embed_dim))


def fact(svc, text, subject, predicate, value, agent="ui", **kw):
    return svc.remember(agent, text, subject=subject, predicate=predicate, value=value, source="user", **kw)["results"][0]


# ------------------------------------------------------------------ STM lifecycle


def test_stm_is_shared_across_a_users_agents(cfg):
    svc = make(cfg)
    try:
        fact(svc, "The user is training for a marathon", "user", "training goal", "marathon", agent="claude-code")
        out = svc.recall("opencode", "what is the user training for")
        assert out["items"] and out["items"][0]["tier"] == "stm"
    finally:
        svc.close()


def test_stm_expiry_slides_with_use_and_is_capped(cfg):
    svc = make(cfg)
    try:
        mid = fact(svc, "The user is reading Dune this week", "user", "current book", "Dune")["memory"]["id"]
        created = svc.ledger.get_memory(mid)["created_at"]
        assert svc.ledger.get_memory(mid)["expires_at"] == pytest.approx(created + 72 * HOUR, abs=1)
        advance(70)
        svc.recall("ui", "what book is the user reading")  # a use near the end buys another day
        assert svc.ledger.get_memory(mid)["expires_at"] == pytest.approx(created + 94 * HOUR, abs=5)
        for _ in range(8):  # keep using it; it still can't outlive the 7-day cap
            advance(20)
            svc.recall("ui", "what book is the user reading")
        assert svc.ledger.get_memory(mid)["expires_at"] <= created + 168 * HOUR + 1
    finally:
        svc.close()


def test_expiry_consolidates_first_then_only_drops_discards(cfg):
    keep_ids = set()

    def answer(items):
        return [{"id": i, "store": "fact" if i in keep_ids else "discard", "importance": 0.4} for i, _ in items]

    svc = make(cfg, answer)
    try:
        keep = fact(svc, "The deploy freeze starts Friday", "deploy", "freeze start", "Friday")["memory"]["id"]
        chat = svc.remember("ui", "lol that meeting was long", type="event", subject="chat", predicate="smalltalk",
                            value="meeting", source="user")["results"][0]["memory"]["id"]  # fmt: skip
        keep_ids.add(keep)
        advance(73)
        stats = svc.sweep()
        assert stats["consolidated"] == 2 and stats["archived"] == 1
        assert svc.ledger.get_memory(keep)["tier"] == "ltm"
        assert svc.ledger.get_memory(chat)["status"] == "archived"
        advance(25)  # grace period over: hard delete, and no tombstone (it may be learned again)
        assert svc.sweep()["purged"] == 1
        assert svc.ledger.get_memory(chat) is None
        again = svc.remember("ui", "lol that meeting was long", type="event", subject="chat", predicate="smalltalk",
                             value="meeting", source="user")["results"][0]  # fmt: skip
        assert again["outcome"] == "created"
    finally:
        svc.close()


def test_tune_stm_suggests_ttl_from_reuse_age(cfg):
    svc = make(cfg)
    try:
        fact(svc, "The user prefers short answers", "user", "answer style", "short")
        svc.recall("ui", "how does the user like answers")
        assert svc.stm_stats()["suggested_h"] is None  # too little data to suggest anything
        for _ in range(24):
            advance(2)
            svc.recall("ui", "how does the user like answers")
        tune = svc.stm_stats()
        assert tune["samples"] == 25 and 48 <= tune["suggested_h"] <= 96
    finally:
        svc.close()


# ------------------------------------------------------------------ consolidation


def test_consolidation_routes_to_stores_and_validates(cfg):
    def answer(items):
        out = []
        for mid, text in items:
            if "Acme" in text:
                out.append({"id": mid, "store": "table", "table_row": {"table": "invoices", "row": {"client": "Acme", "amount": 1200}}})
            elif "Globex" in text:  # wrong columns for an existing table → falls back to rules
                out.append({"id": mid, "store": "table", "table_row": {"table": "invoices", "row": {"vendor": "Globex"}}})
            elif "depends" in text:
                out.append({"id": mid, "store": "graph", "relation": {"src": "rufur", "relation": "depends on", "dst": "sqlite-vec"}})
            elif "dark mode" in text:
                out.append({"id": mid, "store": "profile", "volatility": "static"})
            else:
                out.append({"id": "mem_not_sent", "store": "discard"})  # ids we didn't send are ignored
        return out

    svc = make(cfg, answer)
    try:
        ids = {
            "acme": fact(svc, "Invoice for Acme was 1200 USD", "invoice acme", "amount", "1200")["memory"]["id"],
            "globex": fact(svc, "Invoice for Globex was 800 USD", "invoice globex", "amount", "800")["memory"]["id"],
            "dep": fact(svc, "rufur depends on sqlite-vec", "rufur", "dependency", "sqlite-vec")["memory"]["id"],
            "pref": fact(svc, "The user prefers dark mode", "user", "theme", "dark mode")["memory"]["id"],
            "other": fact(svc, "The office wifi is slow", "office", "wifi", "slow")["memory"]["id"],
        }
        run = svc.consolidate("ui")
        assert run["promoted"] == 5 and run["fallback"] >= 2
        store = {k: svc.ledger.get_memory(v)["store"] for k, v in ids.items()}
        assert store == {"acme": "table", "globex": "fact", "dep": "graph", "pref": "profile", "other": "fact"}
        assert json.loads(svc.db.scalar("SELECT columns FROM table_schemas WHERE table_name = 'invoices'")) == ["amount", "client"]
        assert svc.db.scalar("SELECT count(*) FROM edges WHERE memory_id = ?", (ids["dep"],)) == 1
        # Promotion is in place, and the LLM never touched trust. Its "static" is clamped to "slow":
        # a preference can change, so it still gets a revalidation date.
        m = svc.ledger.get_memory(ids["pref"])
        assert m["tier"] == "ltm" and m["trust"] == "high" and m["volatility"] == "slow" and m["revalidate_at"]
        assert svc.consolidate("ui")["items"] == 0  # nothing left to file
    finally:
        svc.close()


def test_consolidation_without_llm_uses_rules(cfg):
    svc = make(cfg)  # consolidate role unavailable
    try:
        mid = fact(svc, "The user prefers vim", "user", "editor", "vim")["memory"]["id"]
        run = svc.consolidate("ui")
        assert run["fallback"] == 1 and svc.ledger.get_memory(mid)["store"] == "profile"
    finally:
        svc.close()


def test_journal_written_and_removed_by_forget(cfg):
    def extra(role, prompt):
        if role == "consolidate" and "MEMORIES:" not in prompt:
            return {"summary": "The user mentioned living in Tromso and planning a trip.", "topics": ["travel"]}
        return None

    svc = make(cfg, lambda items: [{"id": i, "store": "fact"} for i, _ in items], extra)
    try:
        sid = svc.session_start("claude-code")["session_id"]
        svc.log_event("claude-code", "I live in Tromso", session_id=sid)
        svc.session_end("claude-code", sid)
        run = svc.consolidate("claude-code", journal=True)
        jid = run["journal"]
        assert jid and svc.ledger.get_memory(jid)["store"] == "journal"
        past = svc.recall_past("ui", "journal: the user planning a trip")
        assert any(it["id"] == jid for it in past["items"])
        city = svc.list_memories("ui", type="fact")[0]["id"]
        report = svc.forget("ui", memory_id=city)["report"]
        assert svc.ledger.get_memory(jid) is None and report["completeness"] == 1.0
    finally:
        svc.close()


# ------------------------------------------------------------------ retrieval tiers


def test_working_set_bootstrap_and_dedup(cfg):
    svc = make(cfg, lambda items: [{"id": i, "store": "profile"} for i, _ in items])
    try:
        fact(svc, "The user prefers tabs over spaces", "user", "indentation", "tabs")
        svc.consolidate("ui")
        fact(svc, "Today we are refactoring the billing module", "today", "task", "billing refactor")
        ws = svc.bootstrap("claude-code", project="rufur")
        texts = [it["text"] for it in ws["items"]]
        assert any("tabs" in t for t in texts) and any("billing" in t for t in texts)
        assert "Working memory" in ws["context"]
        hit = svc.recall("claude-code", "does the user prefer tabs or spaces for indentation", session_id=ws["session_id"])
        assert hit["path"] == "working_set"
        again = svc.recall("claude-code", "tabs or spaces indentation", session_id=ws["session_id"], exclude_sent=True)
        assert all("tabs" not in it["text"] for it in again["items"])  # already in context, not re-sent
    finally:
        svc.close()


def test_recall_past_labels_old_versions(cfg):
    svc = make(cfg)
    try:
        fact(svc, "I live in Berlin", "user", "home city", "Berlin")
        svc.remember("ui", "I moved to Munich", source="user")
        now_ = svc.recall("ui", "where does the user live")
        assert all("Berlin" not in it["text"] for it in now_["items"])
        past = svc.recall_past("ui", "where did the user live, Berlin")
        old = next(it for it in past["items"] if "Berlin" in it["text"])
        assert old["valid_to"] and 'current="false"' in past["context"]
    finally:
        svc.close()


def test_stale_items_are_tagged(cfg):
    svc = make(cfg)
    try:
        svc.remember("ui", "This week the user is focused on the demo", type="event", subject="user",
                     predicate="weekly focus", value="demo", source="user")  # fmt: skip
        advance(8 * 24)  # volatile items need revalidation after a week
        out = svc.recall("ui", "what is the user focused on this week", threshold=0.0)
        assert out["items"][0]["stale"] and 'stale="needs confirmation"' in out["context"]
    finally:
        svc.close()


# ------------------------------------------------------------------ subagent pools


def test_subagent_pool_isolation_and_merge_back(cfg):
    svc = make(cfg, lambda items: [{"id": i, "store": "fact", "importance": 0.8} for i, _ in items])
    try:
        fact(svc, "The API rate limit is 100 requests per minute", "api", "rate limit", "100 rpm", agent="claude-code")
        fact(svc, "The user's salary is 90k", "user", "salary", "90k", agent="claude-code", sensitivity="confidential")
        spawn = svc.pool_spawn("claude-code", "write the API client", needs=["API rate limit"])
        sub = svc.agent_for_key(spawn["scoped_key"])
        assert spawn["seeded"] >= 1
        sees = svc.recall(sub, "API rate limit")
        assert sees["items"] and "100" in sees["items"][0]["text"]
        assert svc.recall(sub, "what is the user's salary")["abstained"]  # not seeded, so invisible
        # Long-term memory in the world-readable default pool is invisible too, unless seeded.
        other = fact(svc, "The user lives in Mysore", "user", "home city", "Mysore", agent="claude-code")["memory"]["id"]
        svc.consolidate("claude-code")
        assert svc.ledger.get_memory(other)["tier"] == "ltm"
        assert svc.recall(sub, "where does the user live")["abstained"]
        with pytest.raises(AccessDenied):
            svc.remember(sub, "x", pool="default")
        svc.remember(sub, "The API returns 429 with a Retry-After header", subject="api", predicate="throttle response",
                     value="429 Retry-After", source="agent")  # fmt: skip
        # The parent doesn't see scratch work until the pool closes.
        assert svc.recall("claude-code", "what does the API return when throttled")["abstained"]
        closed = svc.pool_close("claude-code", spawn["pool_id"])
        assert closed["merged"] and closed["merged"][0]["outcome"] == "created"
        back = svc.recall("claude-code", "what does the API return when throttled")
        item = back["items"][0]
        assert "429" in item["text"] and item["tier"] == "stm"
        assert f"from_pool:{spawn['pool_id']}" in svc.ledger.get_memory(item["id"])["flags"]
        assert svc.agent_for_key(spawn["scoped_key"]) is None  # key revoked
    finally:
        svc.close()


def test_expired_scratch_pool_is_closed_by_sweeper(cfg):
    svc = make(cfg)
    try:
        spawn = svc.pool_spawn("claude-code", "quick lookup", ttl_minutes=5)
        advance(1)
        assert svc.sweep()["pools_closed"] == 1
        assert svc.db.scalar("SELECT closed_at FROM pools WHERE id = ?", (spawn["pool_id"],))
    finally:
        svc.close()


# ------------------------------------------------------------------ safety rules


def test_unclear_conflicts_resolved_by_trust(cfg):
    svc = make(cfg)  # classifier answers "unclear" for anything without "moved"
    try:
        doc = svc.remember("ui", "The project budget is 50k", subject="project", predicate="budget", value="50k",
                           source="document")["results"][0]  # fmt: skip
        user = fact(svc, "The project budget is 60k", "project", "budget", "60k")
        assert user["memory"]["status"] == "active"  # higher trust wins outright
        assert svc.ledger.get_memory(doc["memory"]["id"])["status"] == "superseded"
        low = svc.remember("ui", "The project budget is 10k", subject="project", predicate="budget", value="10k",
                           source="document")["results"][0]  # fmt: skip
        assert low["memory"]["status"] == "quarantined"  # lower trust can't contradict the user
        assert any(f.startswith("conflicts_with:") for f in svc.ledger.get_memory(low["memory"]["id"])["flags"])
    finally:
        svc.close()


def test_write_burst_quarantines_agent(cfg):
    cfg.max_writes_per_min = 5
    svc = make(cfg)
    try:
        for i in range(7):
            svc.remember("loopy", f"Fact number {i} about widgets", subject=f"widget {i}", predicate="color",
                         value=f"color{i}", source="agent")  # fmt: skip
        assert svc.agent("loopy")["quarantined"] == 1
        assert svc.recall("ui", "widget color", threshold=0.0)["items"] == []
    finally:
        svc.close()


def test_claims_from_the_users_own_words_dont_trip_the_burst_guard(cfg):
    cfg.max_writes_per_min = 5
    svc = make(cfg)
    try:
        sid = svc.session_start("claude-code")["session_id"]
        for city in ("Oslo", "Bergen", "Tromso", "Pune", "Delhi", "Goa", "Agra"):
            svc.log_event("claude-code", f"I live in {city}", session_id=sid, background=False)
        svc.session_end("claude-code", sid)
        assert svc.db.scalar("SELECT count(*) FROM memories WHERE author_agent = 'claude-code'") >= 7
        assert svc.agent("claude-code")["quarantined"] == 0
    finally:
        svc.close()


def test_sessions_that_produce_no_memory_are_dropped(cfg):
    svc = make(cfg)
    try:
        silent = svc.session_start("claude-code")["session_id"]
        svc.session_end("claude-code", silent)
        chatty = svc.session_start("claude-code")["session_id"]
        ev = svc.log_event("claude-code", "can you fix my bug?", session_id=chatty)
        assert ev["skip_reason"] == "a question or request to the agent, not a statement"
        svc.session_end("claude-code", chatty)
        useful = svc.session_start("claude-code")["session_id"]
        svc.log_event("claude-code", "I live in Tromso", session_id=useful)
        svc.session_end("claude-code", useful)
        abandoned = svc.session_start("claude-code")["session_id"]  # never ended, never used

        left = {r["id"] for r in svc.db.all("SELECT id FROM sessions")}
        assert silent not in left and chatty not in left and useful in left and abandoned in left
        # The transcript survives without its session.
        assert svc.db.scalar("SELECT session_id FROM events WHERE id = ?", (ev["event_id"],)) is None
        advance(25)
        svc.sweep()
        assert abandoned not in {r["id"] for r in svc.db.all("SELECT id FROM sessions")}
    finally:
        svc.close()


def test_independent_corroboration_counts(cfg):
    svc = make(cfg)
    try:
        mid = fact(svc, "The standup is at 10am", "team", "standup time", "10am", agent="claude-code")["memory"]["id"]
        fact(svc, "The standup is at 10am", "team", "standup time", "10am", agent="claude-code")  # same agent: no boost
        assert svc.ledger.get_memory(mid)["corroboration"] == 1
        svc.remember("opencode", "The standup is at 10am", subject="team", predicate="standup time", value="10am",
                     source="document")  # fmt: skip
        assert svc.ledger.get_memory(mid)["corroboration"] == 2
    finally:
        svc.close()


def test_new_tools_have_rest_and_mcp_parity(service, client):
    from rufur.api.mcp_server import LocalBackend, build_mcp

    from .test_mcp import call, scrub

    service.remember("ui", "I live in Berlin", subject="user", predicate="home city", value="Berlin", source="user")
    server = build_mcp(LocalBackend(service), default_agent="ui")
    via_mcp = call(server, "recall_past", {"query": "where did the user live"})
    via_rest = client.post("/v1/recall/past", json={"query": "where did the user live"}, headers={"X-Rufur-Agent": "ui"}).json()
    assert scrub(via_mcp) == scrub(via_rest)
    spawned = call(server, "pool_spawn", {"purpose": "test", "needs": ["home city"]})
    assert client.post(f"/v1/pools/{spawned['pool_id']}/close", json={}, headers={"X-Rufur-Agent": "ui"}).status_code == 200


def test_memory_address_gives_a_small_searchable_pool_to_root_and_subagent(cfg):
    from rufur.service import NotFound

    svc = make(cfg, lambda items: [{"id": i, "store": "fact", "importance": 0.8} for i, _ in items])
    try:
        fact(svc, "The frontend uses React with Tailwind", "frontend", "framework", "React + Tailwind", agent="claude-code")
        fact(svc, "The orders API returns JSON with id, total and status", "orders api", "response shape",
             "id, total, status", agent="claude-code")  # fmt: skip
        fact(svc, "The Postgres replica runs on db2.internal", "postgres", "replica host", "db2.internal", agent="claude-code")
        spawn = svc.pool_spawn("claude-code", "build the orders page (frontend only)",
                               needs=["frontend framework", "orders API response"], seed_budget=200)  # fmt: skip
        addr = spawn["address"]
        assert addr.startswith("mem://") and spawn["seed_tokens"] <= 200
        # Subagent (same identity as its parent, as in Claude Code) and root both search the pool by address.
        assert "React" in svc.pool_search("claude-code", addr, "which frontend framework")["context"]
        assert svc.pool_search("claude-code", addr, "where does the postgres replica run")["abstained"]  # not seeded
        # The address is the capability: another agent can use it, without it nothing.
        assert not svc.pool_search("opencode", addr, "orders API response")["abstained"]
        with pytest.raises(NotFound):
            svc.pool_search("opencode", addr.rsplit("/", 1)[0], "orders API response")
        # Missed something: the memory layer fetches it into the pool.
        assert svc.pool_fetch("claude-code", addr, "postgres replica host")["added"]
        assert "db2" in svc.pool_search("claude-code", addr, "where does the postgres replica run")["context"]
        svc.pool_remember("claude-code", addr, "The orders page polls the API every 30 seconds", subject="orders page",
                          predicate="refresh interval", value="30 seconds")  # fmt: skip
        assert svc.recall("claude-code", "how often does the orders page refresh")["abstained"]  # still pool-only
        svc.pool_close("claude-code", addr)
        assert not svc.recall("claude-code", "how often does the orders page refresh")["abstained"]
        with pytest.raises(NotFound):
            svc.pool_search("claude-code", addr, "anything")  # closed address stops working
    finally:
        svc.close()


# ------------------------------------------------------------------ rerankers


class FakeReranker:
    """Scores by keyword: 0.9 if any keyword is in the doc, else 0.01."""

    name = "fake"

    def __init__(self, *keywords):
        self.keywords = keywords
        self.calls = 0

    def score(self, query, docs, instruction=None):
        self.calls += 1
        return [0.9 if any(k in d.lower() for k in self.keywords) else 0.01 for d in docs]


def test_forget_by_query_uses_reranker_then_judge(cfg):
    def judge(role, prompt):
        if role != "forget" or "MEMORIES:" not in prompt:
            return None  # question rewrite unavailable: falls back to the request minus its verb
        items = re.findall(r"id=(\S+): (.*)", prompt)
        # Forget the address, keep the cafe that only shares the street name and the dog the "I" key-matched.
        return {"items": [{"id": i, "forget": "lives at" in t, "reason": "x"} for i, t in items]}

    svc = make(cfg, extra=judge)
    try:
        home = fact(svc, "The user lives at 12 Baker Street.", "user", "home address", "12 Baker Street")["memory"]["id"]
        fact(svc, "Baker Street Cafe is the user's favourite restaurant.", "baker street cafe", "category", "restaurant")
        other = fact(svc, "The user's dog is called Bruno.", "user", "dog name", "Bruno")["memory"]["id"]
        unrelated = fact(svc, "Project Atlas ships on Friday.", "project atlas", "ship date", "Friday")["memory"]["id"]
        svc.deep_reranker = FakeReranker("baker")
        pre = svc.forget("ui", query="forget where I live")
        assert pre["search"]["reranker"] == "fake" and pre["search"]["judge"] == "llm"
        assert pre["search"]["scope"] == "exhaustive"
        by_id = {c["id"]: c for c in pre["candidates"]}
        assert unrelated not in by_id  # below the rerank floor and not key-matched: never offered
        # "I" key-matches every user fact, so the dog reaches the judge despite its low score; the judge keeps it.
        assert by_id[other]["match"] == "key" and by_id[other]["decision"] == "keep"
        assert by_id[home]["decision"] == "forget"
        assert [c["decision"] for c in pre["candidates"] if "restaurant" in c["text"]] == ["keep"]
        out = svc.forget("ui", query="forget where I live", confirm=True)
        assert [f["id"] for f in out["forgotten"]] == [home]
        assert svc.ledger.get_memory(other) is not None
    finally:
        svc.close()


def test_forget_falls_back_without_reranker(cfg):
    svc = make(cfg)
    try:
        fact(svc, "The user lives at 12 Baker Street.", "user", "home address", "12 Baker Street")
        pre = svc.forget("ui", query="forget my home address")
        assert pre["search"]["reranker"] is None and pre["candidates"]
    finally:
        svc.close()


def test_fast_recall_reranker_reorders(cfg):
    svc = make(cfg)
    try:
        fact(svc, "The user's favourite colour is blue.", "user", "favourite colour", "blue")
        fact(svc, "The user's car colour is red.", "user", "car colour", "red")
        svc.fast_reranker = FakeReranker("car")
        res = svc.recall("ui", "what colour is the user's car?", verify=False)
        assert res["path"].startswith("fast+rerank")
        assert "car" in res["items"][0]["text"]
    finally:
        svc.close()


def test_user_facts_are_not_discarded_and_manual_runs_retry_old_discards(cfg):
    svc = make(cfg, lambda items: [{"id": i, "store": "discard", "importance": 0.4} for i, _ in items])
    try:
        name = fact(svc, "The project's name is Ledgerly", "project", "name", "Ledgerly")["memory"]["id"]
        chat = svc.remember("ui", "lol that meeting was long", type="event", subject="chat", predicate="smalltalk",
                            value="meeting", source="user")["results"][0]["memory"]["id"]  # fmt: skip
        svc.consolidate("ui")
        assert svc.ledger.get_memory(name)["tier"] == "ltm"  # a stated fact survives a wrong "discard"
        assert svc.ledger.get_memory(chat)["store"] == "discard"  # small talk is still dropped
        # An item an older run discarded is skipped by automatic runs but retried from the dashboard.
        svc.db.execute("UPDATE memories SET store = 'discard', consolidation_run = 'run_old' WHERE id = ?", (chat,))
        assert svc.consolidate("ui")["items"] == 0
        assert svc.consolidate("ui", retry_discarded=True)["items"] == 1
    finally:
        svc.close()
