"""Chunking, the salience gate, flat-schema extraction and the cheaper write path."""

from rufur.llm import FakeLLM
from rufur.write.chunk import detect_sensitivity, is_salient, salient_chunks, split_chunks
from rufur.write.extract import extract_claims

from .conftest import fake_handler

UI = {"X-Rufur-Agent": "ui"}


def test_chunks_are_verbatim_bounded_slices():
    text = "I live in Pune, and I prefer tabs. " * 40
    chunks = split_chunks(text, 120)
    assert all(len(c) <= 120 for c in chunks) and len(chunks) > 5
    assert all(c in text for c in chunks)
    assert split_chunks("One. Two. Three.", 400) == ["One. Two. Three."]


def test_gate_keeps_statements_and_drops_chatter():
    keep = ["I live in Bangalore and I prefer dark mode.", "We decided to use PostgreSQL because we need transactions.",
            "The staging server is staging.ledgerly.io.", "API limits: free plan 100 requests/min, pro plan 1000 requests/min.",
            "Update: the deadline slipped to December 22."]  # fmt: skip
    drop = ["run the tests", "Can you fix the bug in main.py?", "thanks, that works", "what does this function do?", "ok"]
    assert all(is_salient(t) for t in keep)
    assert not any(is_salient(t) for t in drop)
    assert not is_salient("Sure, here is the plan for the weekend and some other things.", role="assistant")
    assert is_salient("We decided to use PostgreSQL.", role="assistant")


def test_sensitivity_floor():
    assert detect_sensitivity("my key is AKIAIOSFODNN7EXAMPLE") == "restricted"
    assert detect_sensitivity("I'm on medication for hypertension") == "restricted"
    assert detect_sensitivity("mail me at a@b.dev") == "confidential"
    assert detect_sensitivity("I prefer tabs") == "internal"


def test_extract_skips_the_model_when_nothing_is_salient(cfg):
    calls = []
    llm = FakeLLM(cfg, lambda role, prompt: calls.append(prompt) or {"claims": []})
    events = [{"role": "user", "content": "thanks, run the tests please", "source_type": "user"}]
    accepted, rejected, usage = extract_claims(llm, events)
    assert (accepted, rejected, usage) == ([], [], None) and not calls


def test_extract_batches_and_uses_chunk_as_quote(cfg):
    prompts = []
    llm = FakeLLM(cfg, lambda role, prompt: prompts.append(prompt) or fake_handler(role, prompt))
    cfg.chunk_chars, cfg.extract_batch_chars = 26, 30
    events = [{"role": "user", "content": "I live in Pune. I prefer vim for editing.", "source_type": "user"}]
    accepted, _, _ = extract_claims(llm, events)
    assert len(prompts) == 2  # one chunk per call at this batch size
    assert {c.quote for c, _ in accepted} == {"I live in Pune.", "I prefer vim for editing."}


def test_extract_sensitivity_never_below_pattern(cfg):
    handler = lambda role, prompt: {"claims": [[0, "fact", "user", "aws key", "AKIAIOSFODNN7EXAMPLE"]]}  # noqa: E731
    events = [{"role": "user", "content": "My AWS access key is AKIAIOSFODNN7EXAMPLE", "source_type": "user"}]
    accepted, _, _ = extract_claims(FakeLLM(cfg, handler), events)
    assert accepted[0][0].sensitivity == "restricted"


def test_events_are_gated_and_deduped_at_ingest(service):
    sid = service.session_start("claude-code")["session_id"]
    assert service.log_event("claude-code", "thanks, that works", session_id=sid)["skipped"]
    first = service.log_event("claude-code", "I live in Oslo", session_id=sid)
    again = service.log_event("claude-code", "I live in Oslo", session_id=sid)
    assert not first.get("skipped") and again["skipped"]
    assert service.db.scalar("SELECT count(*) FROM events WHERE session_id = ? AND extracted = 0", (sid,)) == 1


def test_idle_session_is_extracted_without_a_checkpoint(service, cfg):
    sid = service.session_start("claude-code")["session_id"]
    service.log_event("claude-code", "I live in Oslo", session_id=sid)
    assert service.extract_idle_sessions() == 0  # still active
    cfg.extract_idle_s = -1
    assert service.extract_idle_sessions() == 1
    service.worker.submit(lambda: None).result()  # drain the worker
    assert service.db.scalar("SELECT count(*) FROM memories WHERE text LIKE '%Oslo%'") == 1


def test_remember_without_a_key_returns_at_once(service):
    out = service.remember("ui", "I live in Oslo", source="user", wait=False)
    assert out["queued"] and "event_id" in out
    service.worker.submit(lambda: None).result()
    assert service.db.scalar("SELECT count(*) FROM memories WHERE text LIKE '%Oslo%'") == 1
    # An explicit key is cheap, so it still answers with the stored result.
    keyed = service.remember("ui", "The budget is 80k", subject="project", predicate="budget", value="80k", wait=False)
    assert keyed["results"][0]["outcome"] == "created"


def test_unusable_answer_escalates_to_the_next_model(cfg):
    from rufur.config import ProviderRef
    from rufur.llm import LLM, LLMResult
    from rufur.write.extract import FlatExtraction, _well_formed

    bad = {"claims": [[7, "fact", "", "", ""]]}
    good = {"claims": [[0, "fact", "user", "x", "y"]]}
    cfg.roles = {"extract": [ProviderRef("ollama", "small"), ProviderRef("ollama", "big")]}

    class Chain(LLM):
        def _call(self, hop, system, user, schema, num_predict=2048):
            return LLMResult(bad if hop.model == "small" else good, hop.model, 1, 1, 0.0)

    out, usage = Chain(cfg).complete("extract", "s", "u", FlatExtraction, accept=_well_formed(1))
    assert usage.model == "big" and out.claims[0][0] == 0
    # When every model answers badly, the first parsed answer comes back for the caller to reject.
    cfg.roles = {"extract": [ProviderRef("ollama", "small")]}
    out, usage = Chain(cfg).complete("extract", "s", "u", FlatExtraction, accept=_well_formed(1))
    assert usage.model == "small"


def test_rows_become_typed_claims_with_code_built_text(cfg):
    handler = lambda role, prompt: {"claims": [  # noqa: E731
        [0, "relation", "Priya Nair", "manager of", "Aarav"],
        [0, "table", "team_members", "team_members", "name=Rohan Das; city=Pune"],
        ["0", "preference", "user", "preferred theme", "dark mode"],
        [0, "bogus-type", "user", "age", 27],
        [5, "fact", "user", "x", "y"],  # no such chunk
        [0, "fact", "user"]]}  # too short
    events = [{"role": "user", "content": "I prefer dark mode and Rohan Das, backend, Pune joined.", "source_type": "user"}]
    accepted, rejected, _ = extract_claims(FakeLLM(cfg, handler), events)
    by = {c.predicate: c for c, _ in accepted}
    assert by["manager of"].relation.dst == "Aarav"
    assert by["team_members"].table_row.row == {"name": "Rohan Das", "city": "Pune"}
    assert by["preferred theme"].text == "The user's preferred theme is dark mode."
    assert by["age"].type == "fact" and by["age"].value == "27"
    assert len(rejected) == 2


def test_long_rows_from_a_small_model_keep_their_value():
    from rufur.write.extract import _parse_row

    key = _parse_row([0, "fact", "user", "project", "name", "Ledgerly"], 1)
    assert (key.predicate, key.value) == ("project name", "Ledgerly")
    secret = _parse_row([0, "fact", "user", "payment gateway", "test API key", "rzp_test_x", "restricted"], 1)
    assert (secret.value, secret.sensitivity) == ("rzp_test_x", "restricted")
    record = _parse_row([0, "fact", "customer", "name", "Sunrise Clinic", "plan", "Pro"], 1)
    assert (record.type, record.predicate, record.value) == ("table", "customer", "name=Sunrise Clinic; plan=Pro")
