---
name: run-rufur
description: Build, start, run, smoke-test and screenshot the RUFUR memory daemon (REST + MCP + brain UI). Use when asked to run or start rufur, check a change works against the real daemon, call its REST/MCP API, take a screenshot of the brain UI, or run its tests.
---

RUFUR is a local daemon (FastAPI + MCP + a React UI in one process). Drive it with
`.claude/skills/run-rufur/driver.sh`: it boots a **throwaway** daemon on port 7600 with its own
data dir, checks REST and MCP with `curl`, and takes UI screenshots with headless Chromium
(`shot.py`). All paths are relative to the repo root.

**Never touch port 7437 or `~/.rufur`.** That's the user's real daemon and memory. The driver
refuses `RUFUR_PORT=7437`.

## Prerequisites

Tested on macOS (arm64): `uv`, `node`/`npm`, `jq` and `curl`. For the screenshots, a Playwright
Chromium must be in `~/Library/Caches/ms-playwright` (or `~/.cache/ms-playwright`).
Ollama is only needed for `up --live`. It must have `qwen2.5:7b-instruct` and `nomic-embed-text`.

## Setup / build

```bash
uv sync
cd ui && npm install && npm run build && cd ..   # writes the UI into src/rufur/static (driver does this if missing)
```

## Run (agent path)

```bash
D=.claude/skills/run-rufur/driver.sh
$D up              # fresh daemon on :7600, offline: hash embedder, no LLM (fast, deterministic)
$D smoke           # 11 checks; prints "ok …" per check, exits 1 on the first FAIL
$D api POST /v1/memories '{"text":"The user lives in Bangalore","subject":"user","predicate":"lives in","value":"Bangalore","source":"agent"}'
$D api POST /v1/recall '{"query":"where does the user live?"}' opencode   # 4th arg = agent name
$D shot overview sessions tokens forgetting   # PNGs → $RUFUR_HOME/shots/<view>.png, prints paths + console errors
$D down
```

| command | what it does |
|---|---|
| `up [--live]` | wipes `$RUFUR_HOME` and starts the daemon. `--live` uses the real Ollama models (smoke still takes about 6 s). |
| `smoke` | 11 checks: health, remember, cross-agent recall, abstain, bootstrap, MCP `tools/list` + `tools/call recall`, pool spawn/close, consolidate, doctor |
| `api METHOD PATH [JSON] [AGENT]` | one raw REST call as `X-Rufur-Agent: AGENT` (default `claude-code`) |
| `shot VIEW…` | views: `overview sessions tokens forgetting review agents raw` (`sessions` also captures its Timeline and Deleted tabs, `raw` its Vector store and Links tabs). Waits for the "Live" badge (SSE connected). `RUFUR_SHOT_DARK=1` for dark mode, `RUFUR_SHOT_WIDTH=390` for a phone. |
| `down` | `rufur stop` for that home |

Env: `RUFUR_HOME` (default `$TMPDIR/rufur-run`), `RUFUR_PORT` (default `7600`), `RUFUR_SHOTS`.
Daemon log: `$RUFUR_HOME/logs/daemon.log`. Open `Read` on the PNGs to actually look at them.

MCP by hand (stateless JSON, no session handshake needed):

```bash
curl -s http://127.0.0.1:7600/mcp -H 'X-Rufur-Agent: claude-code' -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '[.result.tools[].name]'
```

## Direct invocation (no daemon)

Most changes touch `src/rufur/service.py`, `write/` or `read/`. Call the service in-process:

```bash
uv run python - <<'EOF'
import tempfile
from rufur.config import load_config
from rufur.embed import HashEmbedder
from rufur.service import MemoryService
cfg = load_config(tempfile.mkdtemp(), embed_provider="hash", embed_dim=256, offline=True)
svc = MemoryService(cfg, embedder=HashEmbedder(cfg.embed_dim))
svc.remember("claude-code", "The user prefers pnpm", subject="user", predicate="package manager", value="pnpm")
r = svc.recall("opencode", "which package manager does the user prefer?", verify=False)
print(r["abstained"], [i["text"] for i in r["items"]])
svc.close()
EOF
```

For a deterministic LLM, use the `FakeLLM` and `fake_handler` from `tests/conftest.py`.

## Run (human path)

`uv run rufur start` then `uv run rufur open` uses `~/.rufur` on :7437, which is the user's real memory. Agents don't use it.

## Test

```bash
uv run pytest -q      # 50 passed in ~2 s; offline (fake LLM, hash embedder, test clock)
```

## Gotchas

- **Offline mode doesn't extract.** With `RUFUR_OFFLINE=1`, session-end extraction stays queued, so events → memories does nothing. Write through `POST /v1/memories` with `subject`/`predicate`/`value` (no LLM needed). Test extraction, conflicts and consolidation placement with `up --live`.
- **The hash embedder needs real questions.** Offline, `pool_spawn` with `needs:["rufur database"]` seeded 0 items, but "which database does the rufur project use?" seeded 1. Use full questions in offline checks.
- **`RUFUR_OFFLINE`, `RUFUR_HOME` and `RUFUR_PORT` are read by the background child** that `rufur start` spawns, so they must be exported before `start`, which the driver does. `rufur start` against a live port just prints "already running", so the driver stops it first.
- **Agent-claimed `source:"user"` is stored at medium trust** unless a logged user event contains the text. Use sessions/events to test high-trust user facts (see `docs/TRY_IT.md`).
- **The pip Playwright wants a newer Chromium than the cached one.** `p.chromium.launch()` fails with "Executable doesn't exist … chromium_headless_shell-1254". `shot.py` falls back to any cached `chrome-headless-shell`.
- **End-to-end runs through a real agent CLI on local models are slow.** opencode's ~10k-token system prompt takes a local 12B model minutes, and Ollama swaps models between answering and extraction. Prefer the driver. Ollama `*-cloud` fallbacks need paid credits on this account.

- **The Usage page headline goes negative on a tiny dataset.** After `smoke` it shows "102 tokens … instead of the full 22-token history (-361.8% less)". This is a known UI quirk, not a driver failure.

## Troubleshooting

- **`npm install` fails with `EACCES` under `~/.npm`**: root-owned cache files. Run `npm_config_cache=$TMPDIR/npm-cache npm install`.
- **`zsh: no matches found: …/*`** when globbing a missing dir: run `setopt nonomatch` first, or use bash.
- **`daemon failed to start`**: read `$RUFUR_HOME/logs/daemon.log`.
