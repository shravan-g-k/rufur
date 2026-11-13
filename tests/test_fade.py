"""Intelligent forgetting: unused long-term memories fade, come back when used, and go if they stay unused."""

import pytest

from rufur import textutil
from rufur.embed import HashEmbedder
from rufur.llm import FakeLLM
from rufur.service import MemoryService

from .conftest import fake_handler


@pytest.fixture
def svc(cfg):
    textutil.CLOCK_OFFSET = 0.0
    s = MemoryService(cfg, llm=FakeLLM(cfg, fake_handler), embedder=HashEmbedder(cfg.embed_dim))
    yield s
    s.close()
    textutil.CLOCK_OFFSET = 0.0


def ltm(svc, text, **kw):
    r = svc.remember("ui", text, subject="project", predicate=text.split()[1], value=text, long_term=True, **kw)
    mid = r["results"][0]["memory"]["id"]
    assert svc.ledger.get_memory(mid)["tier"] == "ltm"
    return mid


def test_unused_memory_fades_then_is_purged_without_tombstone(svc):
    mid = ltm(svc, "Kestrel uses Redis for caching")
    textutil.CLOCK_OFFSET = 60 * 86400
    assert svc.sweep()["fading"]["faded"] == [mid]
    m = svc.ledger.get_memory(mid)
    assert m["status"] == "archived" and "fading" in m["flags"]
    assert mid in [f["id"] for f in svc.dashboard_forgetting("ui")["fading"]]

    textutil.CLOCK_OFFSET = 95 * 86400
    assert svc.sweep()["fading"]["purged"] == [mid]
    assert svc.ledger.get_memory(mid) is None
    assert svc.db.scalar("SELECT count(*) FROM tombstones") == 0  # decay, not a user forget: may be learned again


def test_fading_memory_comes_back_when_a_recall_uses_it(svc):
    mid = ltm(svc, "Kestrel uses Redis for caching")
    textutil.CLOCK_OFFSET = 60 * 86400
    svc.sweep()
    out = svc.recall("claude-code", "what does Kestrel use for caching?")
    assert mid in [it["id"] for it in out["items"]]
    m = svc.ledger.get_memory(mid)
    assert m["status"] == "active" and "fading" not in m["flags"]


def test_protected_memories_never_fade(svc):
    pinned = ltm(svc, "Kestrel deploys every Friday", pinned=True)
    textutil.CLOCK_OFFSET = 400 * 86400
    assert pinned not in svc.sweep()["fading"]["faded"]
    assert svc.ledger.get_memory(pinned)["status"] == "active"
