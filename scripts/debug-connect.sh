#!/usr/bin/env bash
# Drive a RUNNING JBrain box's owner debug console from a Claude session using a
# capability token the owner minted in the PWA (Settings → Debug access).
# Decodes the host+key payload and calls /api/debug/* so you don't hand-build
# curl. See docs/runbooks/DEBUG_ACCESS_SESSION_GUIDE.md for the full workflow.
#
# Token source (first found wins):
#   --token <payload>          a one-off, highest priority
#   $JBRAIN_DEBUG_TOKEN        an exported payload
#   ./.jbrain-debug-token      a gitignored file at the repo root (recommended)
#
#   scripts/debug-connect.sh whoami
#   scripts/debug-connect.sh version                   # git rev the running server was built from
#   scripts/debug-connect.sh version-history           # timeline of deployed versions
#   scripts/debug-connect.sh complete --strength high --system "Be terse" "ping"
#   echo "long prompt..." | scripts/debug-connect.sh complete --task agent.turn
#   scripts/debug-connect.sh complete --stream --task agent.turn "hi"  # + ttft_ms & frames
#   scripts/debug-connect.sh vision <attachment_id> --task vision.caption --system "..."
#   scripts/debug-connect.sh sql "select code, name from app.domains"
#   scripts/debug-connect.sh fetch https://example.com/walled --find "keyword"
#   scripts/debug-connect.sh solve https://www.reuters.com/... # force ONLY the byparr solver tier
#   scripts/debug-connect.sh tavily https://example.com/walled # force ONLY the hosted Tavily tier
#   scripts/debug-connect.sh logs api --tail 100
#   scripts/debug-connect.sh host                      # host RAM + per-container + per-process RSS
#   scripts/debug-connect.sh gateway-logs --tail 200   # model engine's own slot lifecycle
#   scripts/debug-connect.sh upstream-logs --tail 400  # llama-server's OWN log (slot lifecycle)
#   scripts/debug-connect.sh drop-cache [ids]          # reclaim stale weights page cache
#   scripts/debug-connect.sh props <model_id>          # engine's own build / n_ctx / total_slots
#   scripts/debug-connect.sh slots <model_id>          # per-slot state; is speculation drafting?
#   scripts/debug-connect.sh spec-metrics <model_id>   # tokens/step + PROMPT-CACHE reuse
#     (`serving-metrics` is the same route. `prompt_tokens_cached_total` is the authoritative
#      reuse signal — /slots' n_prompt_tokens_cache is zeroed on release and reads 0 after any
#      completed request. Both are lifetime totals: delta them around one request.)
#   scripts/debug-connect.sh extra-args <id> --swa-full  # try launch flags live; no args clears
#   scripts/debug-connect.sh ctx <id> 65536            # the served -c
#   scripts/debug-connect.sh prime <id>                # real jerv prime, returns elapsed_ms
#   scripts/debug-connect.sh metrics                   # host telemetry: GPU busy %, power, load
#   scripts/debug-connect.sh llm                       # show live routing
#   scripts/debug-connect.sh llm-set agent.turn gpt-oss-120b high  # bare id, no 'local:'
#   scripts/debug-connect.sh load gpt-oss-120b
#   scripts/debug-connect.sh replay --body-file sitting.json  # multi-turn replay
#   scripts/debug-connect.sh jmolt-status                        # why no night last night?
#   scripts/debug-connect.sh sim harvest "the 2026-08-29 night"  # record the platform
#   scripts/debug-connect.sh sim run <corpus-id> --nights 10     # a scored arm
#   scripts/debug-connect.sh sim run <id> --engine ledger --nights 10  # the other engine
#   scripts/debug-connect.sh raw GET /api/debug/whoami
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"

usage() {
  sed -n '2,35p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

# --- token resolution -------------------------------------------------------
PAYLOAD="${JBRAIN_DEBUG_TOKEN:-}"
if [ "${1:-}" = "--token" ]; then
  PAYLOAD="${2:-}"
  shift 2
fi

# Help needs no token, so handle it before demanding one.
case "${1:-help}" in help | -h | --help) usage 0 ;; esac
if [ -z "$PAYLOAD" ] && [ -f "$REPO/.jbrain-debug-token" ]; then
  PAYLOAD="$(tr -d '[:space:]' <"$REPO/.jbrain-debug-token")"
fi
if [ -z "$PAYLOAD" ]; then
  echo "no token: pass --token <payload>, export JBRAIN_DEBUG_TOKEN, or write .jbrain-debug-token" >&2
  exit 2
fi

# Decode the base64url(JSON{u,k}) payload into BASE + KEY via python (no jq dep).
read -r BASE KEY < <(PAYLOAD="$PAYLOAD" python3 - <<'PY'
import base64, json, os, sys
p = os.environ["PAYLOAD"].strip()
try:
    d = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    print(d["u"], d["k"])
except Exception as exc:  # noqa: BLE001 - a bad paste should fail loud, not crash cryptically
    sys.stderr.write(f"bad token payload: {exc}\n")
    sys.exit(2)
PY
)
[ -n "$BASE" ] && [ -n "$KEY" ] || { echo "could not decode token payload" >&2; exit 2; }

# --- plumbing ---------------------------------------------------------------
# Pretty-print JSON when the body parses as JSON; pass plain text (logs) through.
_pp() { python3 -c 'import sys,json; d=sys.stdin.read();
try: print(json.dumps(json.loads(d), indent=2))
except Exception: sys.stdout.write(d)'; }

# Fails LOUDLY on a non-2xx, and that is not politeness — it is a safety property.
#
# This used to print the body and exit 0 whatever the status, so a refusal looked exactly
# like a success: `llm-set agent.turn local:gpt-oss-120b` returns 422 "unknown provider"
# (ids are bare, with no `local:` prefix), the routing silently did not change, and the
# caller went on believing it had. MEASURED cost on 2026-08-21: two loads of a 120B on top
# of an already-resident model, on a box whose documented failure mode is a reclaim livelock
# that needs a power cycle. A control call that quietly does nothing is worse than one that
# errors, because the next step is taken on a false premise.
#
# The body still prints — a 422 detail is the most useful thing on screen — and the status
# goes to stderr with a non-zero exit so `set -e` and any `||` guard actually catch it.
_call() { # METHOD PATH [JSON_BODY]
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS -X "$method" -H "Authorization: Bearer $KEY" -w '\n%{http_code}')
  [ -n "$body" ] && args+=(-H "Content-Type: application/json" -d "$body")
  local out code
  out="$(curl "${args[@]}" "$BASE$path")" || { echo "$out"; echo "curl failed: $method $path" >&2; return 1; }
  code="${out##*$'\n'}"       # the -w status on the last line
  printf '%s' "${out%$'\n'*}" # ...stripped back off the body
  case "$code" in
    2*) return 0 ;;
    *)  echo >&2; echo "HTTP $code from $method $path" >&2; return 1 ;;
  esac
}

# Read the prompt/SQL text: remaining args if present, else stdin (for heredocs
# and pipes). Lets you paste multi-line prompts without shell-quoting hell.
_text_arg() { if [ "$#" -gt 0 ]; then printf '%s' "$*"; else cat; fi; }

cmd="${1:-help}"
[ "$#" -gt 0 ] && shift || true

case "$cmd" in
  whoami) _call GET /api/debug/whoami | _pp ;;

  version) _call GET /api/debug/version | _pp ;;  # git rev the running server was built from

  version-history) # [--limit N] — recorded history of deployed versions, newest first
    lim=50
    [ "${1:-}" = "--limit" ] && { lim="$2"; shift 2; }
    _call GET "/api/debug/version/history?limit=$lim" | _pp
    ;;

  complete) # [--stream] [--system S] [--task T] [--strength S] [--max-tokens N] "text"
    # --stream runs the turn through the STREAMING adapter path — the one a real chat turn
    # takes — and returns `ttft_ms` plus a timestamped frame per streamed part. That is how
    # you see WHERE a slow turn's time went: a long first gap is prefill, an even cadence
    # after it is generation. The frames are buffered on the box, not streamed over the wire,
    # so this survives the proxy timeout that kills a long held-open request.
    SYSTEM="" TASK="" STRENGTH="" MAXTOK="" SCHEMA="" STREAM=""
    while [ "${1:-}" != "" ]; do
      case "$1" in
        --system) SYSTEM="$2"; shift 2 ;;
        --task) TASK="$2"; shift 2 ;;
        --strength) STRENGTH="$2"; shift 2 ;;
        --max-tokens) MAXTOK="$2"; shift 2 ;;
        --json-schema) SCHEMA="$2"; shift 2 ;;  # a JSON Schema string
        --stream) STREAM=1; shift ;;
        --) shift; break ;;
        -*) echo "unknown flag: $1" >&2; exit 2 ;;
        *) break ;;
      esac
    done
    # Via a FILE, not an environment variable. The prompt is the one input here with no
    # natural size bound — a long-context probe is the whole point of a box serving 256k
    # windows — and env + argv share a fixed limit a real one blows straight through
    # ("Argument list too long", measured at ~180 KB).
    _tf="$(mktemp)"; trap 'rm -f "$_tf"' EXIT
    _text_arg "$@" > "$_tf"
    body="$(SYSTEM="$SYSTEM" TASK="$TASK" STRENGTH="$STRENGTH" MAXTOK="$MAXTOK" \
            SCHEMA="$SCHEMA" STREAM="$STREAM" TEXTFILE="$_tf" python3 - <<'PY'
import json, os
with open(os.environ["TEXTFILE"], encoding="utf-8") as f:
    b = {"user_text": f.read()}
for key, env in (("system","SYSTEM"),("task","TASK"),("strength","STRENGTH")):
    if os.environ.get(env): b[key] = os.environ[env]
if os.environ.get("MAXTOK"): b["max_tokens"] = int(os.environ["MAXTOK"])
if os.environ.get("SCHEMA"): b["json_schema"] = json.loads(os.environ["SCHEMA"])
if os.environ.get("STREAM"): b["stream"] = True
print(json.dumps(b))
PY
)"
    _call POST /api/debug/complete "$body" | _pp
    ;;

  tool-probe) # [--task agent.turn] [--tools a,b,c] [--raw-tools-file f.json] [--system "<prompt>"] [--max-tokens N] "<user text>"
    # Send a chosen set of tool SCHEMAS to the routed model and return its PROPOSED tool calls —
    # no handler runs. For bisecting tool-calling crashes (e.g. does gpt-oss die at N tools?):
    # vary --tools and watch which set returns an `error`. --raw-tools-file sends INLINE mutated
    # schemas ([{name,description,input_schema}, ...]) so you can bisect which schema construct
    # crashes the gateway's tool-grammar builder (strip a field/enum/punctuation, re-probe).
    SYSTEM="" TASK="" TOOLS="" MAXTOK="" RAWFILE=""
    while [ "${1:-}" != "" ]; do
      case "$1" in
        --system) SYSTEM="$2"; shift 2 ;;
        --task) TASK="$2"; shift 2 ;;
        --tools) TOOLS="$2"; shift 2 ;;   # comma-separated registry tool names
        --raw-tools-file) RAWFILE="$2"; shift 2 ;;  # JSON file: [{name,description,input_schema}, ...]
        --max-tokens) MAXTOK="$2"; shift 2 ;;
        --) shift; break ;;
        -*) echo "unknown flag: $1" >&2; exit 2 ;;
        *) break ;;
      esac
    done
    USERTEXT="$(_text_arg "$@")"
    body="$(SYSTEM="$SYSTEM" TASK="$TASK" TOOLS="$TOOLS" MAXTOK="$MAXTOK" RAWFILE="$RAWFILE" USERTEXT="$USERTEXT" python3 - <<'PY'
import json, os
b = {"user_text": os.environ["USERTEXT"] or "Use one of your tools to help me."}
if os.environ.get("SYSTEM"): b["system"] = os.environ["SYSTEM"]
if os.environ.get("TASK"): b["task"] = os.environ["TASK"]
if os.environ.get("MAXTOK"): b["max_tokens"] = int(os.environ["MAXTOK"])
b["tools"] = [t for t in os.environ.get("TOOLS", "").split(",") if t.strip()]
raw = os.environ.get("RAWFILE", "")
if raw:
    with open(raw, encoding="utf-8") as fh:
        b["raw_tools"] = json.load(fh)
print(json.dumps(b))
PY
)"
    _call POST /api/debug/tool-probe "$body" | _pp
    ;;

  vision) # <attachment_id> [--task vision.caption|vision.ocr] [--system "<prompt>"] [--max-tokens N]
    ATT="${1:-}"; [ -n "$ATT" ] || { echo "usage: debug-connect.sh vision <attachment_id> [--task ...] [--system ...]" >&2; exit 2; }
    shift
    SYSTEM="" TASK="" MAXTOK=""
    while [ "${1:-}" != "" ]; do
      case "$1" in
        --task) TASK="$2"; shift 2 ;;
        --system) SYSTEM="$2"; shift 2 ;;
        --max-tokens) MAXTOK="$2"; shift 2 ;;
        *) echo "unknown flag: $1" >&2; exit 2 ;;
      esac
    done
    body="$(ATT="$ATT" SYSTEM="$SYSTEM" TASK="$TASK" MAXTOK="$MAXTOK" python3 - <<'PY'
import json, os
b = {"attachment_id": os.environ["ATT"]}
if os.environ.get("TASK"): b["task"] = os.environ["TASK"]
if os.environ.get("SYSTEM"): b["system"] = os.environ["SYSTEM"]
if os.environ.get("MAXTOK"): b["max_tokens"] = int(os.environ["MAXTOK"])
print(json.dumps(b))
PY
)"
    _call POST /api/debug/vision "$body" | _pp
    ;;

  sql)
    SQL="$(_text_arg "$@")"
    [ -n "$SQL" ] || { echo "usage: debug-connect.sh sql '<select ...>'" >&2; exit 2; }
    body="$(SQL="$SQL" python3 -c 'import json,os; print(json.dumps({"sql": os.environ["SQL"]}))')"
    _call POST /api/debug/sql "$body" | _pp
    ;;

  fetch) # <url> [--offset N] [--find TERM] — run a URL through the live direct→reader→solver path
    URL="${1:-}"; [ -n "$URL" ] || { echo "usage: debug-connect.sh fetch <url> [--offset N] [--find TERM]" >&2; exit 2; }
    shift
    OFF=0 FIND=""
    while [ "${1:-}" != "" ]; do
      case "$1" in
        --offset) OFF="$2"; shift 2 ;;
        --find) FIND="$2"; shift 2 ;;
        -*) echo "unknown flag: $1" >&2; exit 2 ;;
        *) break ;;
      esac
    done
    body="$(URL="$URL" OFF="$OFF" FIND="$FIND" python3 -c 'import json,os; print(json.dumps({"url": os.environ["URL"], "offset": int(os.environ["OFF"]), "find": os.environ["FIND"]}))')"
    _call POST /api/debug/fetch "$body" | _pp
    ;;

  solve) # <url> [--offset N] [--find TERM] — force ONLY the byparr solver tier (skip direct+reader)
    URL="${1:-}"; [ -n "$URL" ] || { echo "usage: debug-connect.sh solve <url> [--offset N] [--find TERM]" >&2; exit 2; }
    shift
    OFF=0 FIND=""
    while [ "${1:-}" != "" ]; do
      case "$1" in
        --offset) OFF="$2"; shift 2 ;;
        --find) FIND="$2"; shift 2 ;;
        -*) echo "unknown flag: $1" >&2; exit 2 ;;
        *) break ;;
      esac
    done
    body="$(URL="$URL" OFF="$OFF" FIND="$FIND" python3 -c 'import json,os; print(json.dumps({"url": os.environ["URL"], "offset": int(os.environ["OFF"]), "find": os.environ["FIND"]}))')"
    _call POST /api/debug/solve "$body" | _pp
    ;;

  tavily) # <url> [--offset N] [--find TERM] — force ONLY the hosted Tavily Extract tier
    URL="${1:-}"; [ -n "$URL" ] || { echo "usage: debug-connect.sh tavily <url> [--offset N] [--find TERM]" >&2; exit 2; }
    shift
    OFF=0 FIND=""
    while [ "${1:-}" != "" ]; do
      case "$1" in
        --offset) OFF="$2"; shift 2 ;;
        --find) FIND="$2"; shift 2 ;;
        -*) echo "unknown flag: $1" >&2; exit 2 ;;
        *) break ;;
      esac
    done
    body="$(URL="$URL" OFF="$OFF" FIND="$FIND" python3 -c 'import json,os; print(json.dumps({"url": os.environ["URL"], "offset": int(os.environ["OFF"]), "find": os.environ["FIND"], "tier": "tavily"}))')"
    _call POST /api/debug/fetch "$body" | _pp
    ;;

  logs)
    svc="${1:-}"; [ -n "$svc" ] || { echo "usage: debug-connect.sh logs <service> [--tail N]" >&2; exit 2; }
    shift
    tail=200
    [ "${1:-}" = "--tail" ] && { tail="$2"; shift 2; }
    _call GET "/api/debug/logs/$svc?tail=$tail"
    ;;

  host) _call GET /api/debug/host | _pp ;;   # host memory/swap/disk/load + per-container + per-process RSS

  gateway-logs) # [--tail N] — the model engine's OWN stdout (slot lifecycle), not the container log
    tail=200
    [ "${1:-}" = "--tail" ] && { tail="$2"; shift 2; }
    _call GET "/api/debug/llm/gateway-logs?tail=$tail"
    ;;

  drop-cache) # [<id,id>] — release the page-cache copy of model weights (all models if omitted)
    q=""; [ -n "${1:-}" ] && q="?models=$1"
    _call POST "/api/debug/llm/drop-page-cache$q" | _pp
    ;;

  upstream-logs) # [<stream>] [--tail N] — llama-server's OWN log; <stream> = upstream (default) or a model id
    stream=upstream
    case "${1:-}" in ""|--tail) ;; *) stream="$1"; shift ;; esac
    tail=400
    [ "${1:-}" = "--tail" ] && { tail="$2"; shift 2; }
    _call GET "/api/debug/llm/upstream-logs?stream=$stream&tail=$tail"
    ;;

  props) # <model_id> — the engine's OWN build / n_ctx / total_slots (RESIDENT models only)
    m="${1:?usage: debug-connect.sh props <model_id>}"
    _call GET "/api/debug/llm/local-models/$m/props" | _pp
    ;;

  slots) # <model_id> — per-slot state; the ONLY reliable "is speculation drafting?" signal
    m="${1:?usage: debug-connect.sh slots <model_id>}"
    _call GET "/api/debug/llm/local-models/$m/slots" | _pp
    ;;

  spec-metrics|serving-metrics) # <model_id> — speculation AND prompt-cache reuse counters
    m="${1:?usage: debug-connect.sh spec-metrics <model_id>}"
    _call GET "/api/debug/llm/local-models/$m/metrics" | _pp
    ;;

  extra-args) # <model_id> [flag ...] — try llama-server launch flags live; no args CLEARS them
    m="${1:?usage: debug-connect.sh extra-args <model_id> [flag ...]}"; shift
    body="$(ARGS="$*" python3 -c 'import json,os,shlex
print(json.dumps({"args": shlex.split(os.environ["ARGS"])}))')"
    _call PUT "/api/debug/llm/local-models/$m/extra-args" "$body" | _pp
    ;;

  ctx) # <model_id> <tokens> — the served -c, re-stamped and applied on the model's next load
    m="${1:?usage: debug-connect.sh ctx <model_id> <tokens>}"
    n="${2:?usage: debug-connect.sh ctx <model_id> <tokens>}"
    _call PUT "/api/debug/llm/local-models/$m/context-window" "{\"context_window\": $n}" | _pp
    ;;

  prime) # <model_id> — run the real jerv prime and return elapsed_ms: the measurement instrument
    m="${1:?usage: debug-connect.sh prime <model_id>}"
    _call POST "/api/debug/llm/local-models/$m/prime" '{}' | _pp
    ;;

  # NB: the `-np` parallel-slot count has NO command here on purpose. Its route
  # (`PUT /api/settings/llm/local-models/{id}/parallel-slots`) is owner-authenticated, not a
  # capability-token route, so a debug token cannot set it — it is a PWA action (Settings →
  # LLM → On-box models). A speculative model is clamped to one slot in the config generator
  # regardless, so a stale override there cannot break it.

  metrics | gpu) _call GET /api/debug/host/metrics | _pp ;;  # host telemetry: GPU busy %, power, load

  llm) _call GET /api/debug/llm | _pp ;;

  llm-set) # <task> <provider:spec> [effort]
    task="${1:-}"; prov="${2:-}"; effort="${3:-}"
    [ -n "$task" ] && [ -n "$prov" ] || { echo "usage: debug-connect.sh llm-set <task> <provider> [effort]" >&2; exit 2; }
    body="$(TASK="$task" PROV="$prov" EFFORT="$effort" python3 - <<'PY'
import json, os
entry = {"provider": os.environ["PROV"]}
if os.environ.get("EFFORT"): entry["reasoning_effort"] = os.environ["EFFORT"]
print(json.dumps({"tasks": {os.environ["TASK"]: entry}}))
PY
)"
    _call PUT /api/debug/llm "$body" | _pp
    ;;

  load)   m="${1:?usage: debug-connect.sh load <model_id>}";   _call POST "/api/debug/llm/local-models/$m/load" | _pp ;;
  unload) m="${1:?usage: debug-connect.sh unload <model_id>}"; _call POST "/api/debug/llm/local-models/$m/unload" | _pp ;;

  replay) # --body-file f.json — multi-turn sitting replay against recorded tool results
    # Drives a jmolt sitting PAST its first move by feeding back the results the night
    # actually observed, so a prompt change can be measured against the decision it is meant
    # to affect. `tool-probe` returns one call and jmolt's opening move is pinned by the
    # prologue, so nothing later is reachable with it. Build the body with
    # scripts/jmolt-replay-build.py (it pulls a real sitting out of agent_turns).
    BODYFILE=""
    while [ "${1:-}" != "" ]; do
      case "$1" in
        --body-file) BODYFILE="$2"; shift 2 ;;
        --) shift; break ;;
        -*) echo "unknown flag: $1" >&2; exit 2 ;;
        *) break ;;
      esac
    done
    [ -n "$BODYFILE" ] || { echo "usage: debug-connect.sh replay --body-file <f.json>" >&2; exit 2; }
    _call POST /api/debug/replay "$(cat "$BODYFILE")" | _pp
    ;;

  jmolt-status) # why jmolt did or did not run last night — switch states, never a secret
    _call GET /api/debug/jmolt/status "" | _pp
    ;;

  sim) # harvest [note] | corpora | run <corpus-id> [--nights N] [--label L] [--advisory TEXT] | purge
    # The jmolt simulator (docs/plans/JMOLT_LEDGER_ENGINE_PLAN.md, S1): a night against a
    # RECORDED platform, in seconds, so a design change is measured rather than reasoned
    # about. Nothing here can reach Moltbook with a write — the harvest calls read methods
    # only, and a run drives a client with no credential and no transport whose staged rows
    # carry a flag the live drip's own query cannot see.
    sub="${1:-}"; shift || true
    case "$sub" in
      harvest)
        body="$(NOTE="${1:-}" python3 -c 'import json,os; print(json.dumps({"note": os.environ["NOTE"]}))')"
        _call POST /api/debug/jmolt-sim/harvest "$body" | _pp
        ;;
      corpora) _call GET /api/debug/jmolt-sim/corpora "" | _pp ;;
      purge)   _call POST /api/debug/jmolt-sim/purge "" | _pp ;;
      run)
        CID="${1:?usage: debug-connect.sh sim run <corpus-id> [--nights N] [--label L]}"; shift
        NIGHTS=1; LABEL="arm"; ENGINE="sittings"; ADVISORY=""; HAS_ADVISORY=0
        while [ "${1:-}" != "" ]; do
          case "$1" in
            --nights) NIGHTS="$2"; shift 2 ;;
            --label) LABEL="$2"; shift 2 ;;
            --engine) ENGINE="$2"; shift 2 ;;
            # Omit --advisory entirely for a BASELINE: the arm then sees whatever note the
            # box actually holds, rather than one silently blanked by the harness.
            --advisory) ADVISORY="$2"; HAS_ADVISORY=1; shift 2 ;;
            *) echo "unknown flag: $1" >&2; exit 2 ;;
          esac
        done
        body="$(CID="$CID" NIGHTS="$NIGHTS" LABEL="$LABEL" ENGINE="$ENGINE" \
          ADV="$ADVISORY" HAS="$HAS_ADVISORY" \
          python3 -c 'import json,os
b = {"corpus_id": os.environ["CID"], "nights": int(os.environ["NIGHTS"]),
     "label": os.environ["LABEL"], "engine": os.environ["ENGINE"]}
if os.environ["HAS"] == "1":
    b["advisory"] = os.environ["ADV"]
print(json.dumps(b))')"
        _call POST /api/debug/jmolt-sim/run "$body" | _pp
        ;;
      *) echo "usage: debug-connect.sh sim harvest|corpora|run|purge" >&2; exit 2 ;;
    esac
    ;;

  raw) # METHOD PATH [JSON_BODY] — escape hatch for anything not wrapped above
    method="${1:?usage: debug-connect.sh raw <METHOD> <path> [body]}"
    path="${2:?usage: debug-connect.sh raw <METHOD> <path> [body]}"
    _call "$method" "$path" "${3:-}" | _pp
    ;;

  help|-h|--help) usage 0 ;;
  *) echo "unknown command: $cmd" >&2; usage 2 ;;
esac
