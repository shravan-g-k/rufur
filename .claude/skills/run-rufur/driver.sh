#!/usr/bin/env bash
# Drive a throwaway RUFUR daemon: boot it, poke REST + MCP, screenshot the UI, stop it.
# Never touches the user's real daemon (~/.rufur on :7437).
#
#   driver.sh up [--live]        start a fresh daemon (offline: hash embedder, no LLM; --live: real Ollama models)
#   driver.sh smoke              REST + MCP round trip; exits non-zero on the first failed check
#   driver.sh api METHOD PATH [JSON] [AGENT]   one raw request (AGENT defaults to claude-code)
#   driver.sh shot [VIEW ...]    screenshot UI views (overview sessions tokens forgetting review agents) to $RUFUR_SHOTS
#   driver.sh down               stop the daemon
#
# Env: RUFUR_HOME (default $TMPDIR/rufur-run), RUFUR_PORT (default 7600), RUFUR_SHOTS (default $RUFUR_HOME/shots)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
UNIT=$(cd "$HERE/../../.." && pwd)
export RUFUR_HOME=${RUFUR_HOME:-${TMPDIR:-/tmp}/rufur-run}
export RUFUR_PORT=${RUFUR_PORT:-7600}
export RUFUR_SHOTS=${RUFUR_SHOTS:-$RUFUR_HOME/shots}
B=http://127.0.0.1:$RUFUR_PORT
cd "$UNIT"

if [ "$RUFUR_PORT" = 7437 ]; then echo "refusing to drive :7437 (the user's real daemon)" >&2; exit 2; fi

api() {  # METHOD PATH [JSON] [AGENT]
  local m=$1 p=$2 body=${3:-} agent=${4:-claude-code}
  if [ -n "$body" ]; then
    curl -sS -X "$m" "$B$p" -H "X-Rufur-Agent: $agent" -H 'content-type: application/json' -d "$body"
  else
    curl -sS -X "$m" "$B$p" -H "X-Rufur-Agent: $agent"
  fi
}
check() {  # NAME JQ-EXPR JSON
  if echo "$3" | jq -e "$2" >/dev/null 2>&1; then echo "ok   $1"; else echo "FAIL $1"; echo "$3" | head -c 800; echo; exit 1; fi
}

case "${1:-}" in
  up)
    uv run rufur stop >/dev/null 2>&1 || true
    rm -rf "$RUFUR_HOME"
    if [ "${2:-}" = --live ]; then unset RUFUR_OFFLINE; else export RUFUR_OFFLINE=1; fi
    [ -f src/rufur/static/index.html ] || (cd ui && npm install && npm run build)
    uv run rufur start
    api GET /v1/health | jq -c '{status, home, embedder, offline}'
    ;;
  smoke)
    h=$(api GET /v1/health); check health '.status == "ok"' "$h"
    r=$(api POST /v1/memories '{"text":"The rufur project uses SQLite because it must work offline","subject":"rufur","predicate":"database","value":"SQLite","source":"agent"}')
    check remember '.results[0].memory.id' "$r"
    r=$(api POST /v1/recall '{"query":"which database does the rufur project use?"}' opencode)
    check "recall from another agent" '[.items[].text] | any(test("SQLite"))' "$r"
    r=$(api POST /v1/recall '{"query":"what is the users blood type?"}')
    check "abstains when unknown" '.abstained == true' "$r"
    r=$(api POST /v1/bootstrap '{"project":"rufur"}')
    check "bootstrap working set" '.session_id and (.context | test("SQLite"))' "$r"
    r=$(curl -sS "$B/mcp" -H 'X-Rufur-Agent: claude-code' -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
        -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
    check "mcp tools/list" '[.result.tools[].name] | index("recall") and index("pool_spawn")' "$r"
    r=$(curl -sS "$B/mcp" -H 'X-Rufur-Agent: claude-code' -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
        -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"recall","arguments":{"query":"rufur database"}}}')
    check "mcp tools/call recall" '.result.content[0].text | test("SQLite")' "$r"
    r=$(api POST /v1/pools/spawn '{"purpose":"smoke","needs":["which database does the rufur project use?"]}')
    check "pool_spawn seeds it" '.scoped_key and .seeded >= 1' "$r"
    r=$(api POST "/v1/pools/$(echo "$r" | jq -r .pool_id)/close" '{}')
    check "pool_close" '.status? // true' "$r"
    r=$(api POST /v1/consolidate '{}')
    check consolidate '.items >= 1' "$r"
    r=$(api GET /v1/doctor); check doctor 'type == "object"' "$r"
    echo "smoke passed against $B"
    ;;
  api) shift; api "$@"; echo ;;
  shot) shift; uv run --no-project --with playwright python "$HERE/shot.py" "$B" "$RUFUR_SHOTS" "${@:-overview}" ;;
  down) uv run rufur stop ;;
  *) sed -n 2,12p "$0"; exit 2 ;;
esac
