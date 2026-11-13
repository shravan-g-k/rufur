"""LongMemEval-lite: knowledge updates buried in filler, answered with and without RUFUR.

    uv run python scripts/longmemeval_lite.py [--hash] [--out docs/results/longmemeval_lite.md]

Arms, on the same history and questions (no LLM judge; gold value must appear, stale value must not):
  A. no memory      the last WINDOW lines of the history, what a plain agent still sees
  B. RUFUR, qwen    every write-time decision made by qwen2.5:7b
  C. RUFUR, hybrid  clear yes/no and pick-one decisions made by Kev (decision model), the rest by qwen
Also reports write latency for the updates (where the conflict decision runs) and who made each decision.
"""

from __future__ import annotations

import argparse
import statistics as st
import tempfile
import time

from rufur.config import load_config
from rufur.embed import HashEmbedder, OllamaEmbedder
from rufur.llm import LLM
from rufur.service import MemoryService

U, WINDOW = "ui", 25
# (subject, predicate, old, new, how it changed)
ATTRS = [
    ("user", "home city", "Bangalore", "Mysore", "moved to Mysore"),
    ("user", "employer", "Infosys", "Zoho", "switched jobs to Zoho"),
    ("user", "preferred editor", "Helix", "Neovim", "switched to Neovim"),
    ("rufur", "database choice", "SQLite", "Postgres", "changed to Postgres"),
    ("hackathon demo", "deadline", "November 20", "November 27", "postponed to November 27"),
    ("user", "manager", "Priya", "Rahul", "now reports to Rahul"),
    ("user", "car", "Honda City", "Tata Nexon", "bought a Tata Nexon"),
    ("user", "gym", "Cult Fit", "Anytime Fitness", "moved to Anytime Fitness"),
]
STABLE = [("user", "birthday month", "March"), ("rufur", "license", "Apache-2.0"), ("user", "pet", "a beagle named Biscuit")]
UNANSWERABLE = ["What is the user's blood type?", "What is the name of the user's sister?"]


def run_arm(name: str, decision_model: str, embed_hash: bool, history: list[tuple]) -> dict:
    over = {"embed_provider": "hash", "embed_dim": 256} if embed_hash else {}
    cfg = load_config(tempfile.mkdtemp(), decision_model=decision_model, **over)
    embedder = HashEmbedder(cfg.embed_dim) if embed_hash else OllamaEmbedder(cfg)
    llm = LLM(cfg)
    who = {"kev": 0, "qwen": 0}
    decide, complete = llm.decide, llm.complete

    def counted_decide(*a, **k):
        out = decide(*a, **k)
        who["kev"] += 1
        return out

    def counted_complete(role, *a, **k):
        who["qwen"] += role == "classify"
        return complete(role, *a, **k)

    llm.decide, llm.complete = counted_decide, counted_complete
    svc = MemoryService(cfg, llm=llm, embedder=embedder)
    upd_ms, all_ms = [], []
    for text, subj, pred, val, is_update in history:
        t = time.perf_counter()
        svc.remember(U, text, subject=subj, predicate=pred, value=val, source="user")
        all_ms.append((time.perf_counter() - t) * 1000)
        if is_update:
            upd_ms.append(all_ms[-1])
    res = score(lambda q: (lambda o: "" if o["abstained"] else o["context"])(svc.recall("agent", q, token_budget=300)))
    res |= {"upd_p50": st.median(upd_ms), "upd_p95": sorted(upd_ms)[int(len(upd_ms) * 0.95) - 1], "who": who,
            "disputed": svc.db.scalar("SELECT count(*) FROM memories WHERE status = 'disputed'")}
    svc.close()
    return res


def questions():
    qs = [(f"What is the {s}'s {p}?", new, old) for s, p, old, new, _ in ATTRS]
    qs += [(f"What is the {s}'s {p}?", v, None) for s, p, v in STABLE]
    return qs


def score(ask) -> dict:
    ok = stale = 0
    for q, must, forbid in questions():
        ctx = ask(q).lower()
        ok += must.lower() in ctx
        stale += bool(forbid) and forbid.lower() in ctx
    abstain = sum(not ask(q) for q in UNANSWERABLE)
    return {"correct": ok, "of": len(questions()), "stale": stale, "abstain": abstain}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hash", action="store_true")
    ap.add_argument("--out")
    args = ap.parse_args()

    history, lines = [], []  # history: remember() calls; lines: what a plain agent saw, in order
    filler = iter(f"Small talk turn {i}: weather, lunch plans, a meme about cats." for i in range(10_000))

    def add(text, s, p, v, upd=False):
        history.append((text, s, p, v, upd))
        lines.append(text)
        lines.extend(next(filler) for _ in range(3))

    for s, p, old, _, _ in ATTRS:
        add(f"The {s}'s {p} is {old}.", s, p, old)
    for s, p, v in STABLE:
        add(f"The {s}'s {p} is {v}.", s, p, v)
    add(f"The {ATTRS[0][0]}'s {ATTRS[0][1]} is {ATTRS[0][2]}, as said before.", *ATTRS[0][:2], ATTRS[0][2])  # duplicate
    for s, p, old, new, how in ATTRS:
        add(f"Update: the {s} {how}. The {s}'s {p} is now {new}.", s, p, new, True)

    window = "\n".join(lines[-WINDOW:]).lower()
    a = {"correct": sum(m.lower() in window for _, m, _ in questions()), "of": len(questions()),
         "stale": sum(bool(f) and f.lower() in window for _, _, f in questions()), "abstain": len(UNANSWERABLE)}
    rows = [("A  no memory (last %d lines)" % WINDOW, a)]
    for name, model in (("B  RUFUR, qwen only", ""), ("C  RUFUR, Kev + qwen", load_config(tempfile.mkdtemp()).decision_model)):
        rows.append((name, run_arm(name, model, args.hash, history)))

    out = [f"| arm | correct | stale leaks | abstain ok | update write p50 / p95 ms | decisions kev / qwen | disputed |",
           "|---|---|---|---|---|---|---|"]
    for name, r in rows:
        lat = f"{r['upd_p50']:.0f} / {r['upd_p95']:.0f}" if "upd_p50" in r else "–"
        who = f"{r['who']['kev']} / {r['who']['qwen']}" if "who" in r else "–"
        out.append(f"| {name} | {r['correct']}/{r['of']} | {r['stale']} | {r['abstain']}/{len(UNANSWERABLE)} | {lat} | {who} | {r.get('disputed', '–')} |")
    text = "\n".join(out)
    print(text)
    if args.out:
        open(args.out, "w").write("# LongMemEval-lite\n\n" + text + "\n")


if __name__ == "__main__":
    main()
