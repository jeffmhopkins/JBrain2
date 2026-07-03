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
#   scripts/debug-connect.sh complete --strength high --system "Be terse" "ping"
#   echo "long prompt..." | scripts/debug-connect.sh complete --task agent.turn
#   scripts/debug-connect.sh vision <attachment_id> --task vision.caption --system "..."
#   scripts/debug-connect.sh sql "select code, name from app.domains"
#   scripts/debug-connect.sh logs api --tail 100
#   scripts/debug-connect.sh host                      # host RAM + per-container + per-process RSS
#   scripts/debug-connect.sh gateway-logs --tail 200   # model engine's own slot lifecycle
#   scripts/debug-connect.sh metrics                   # host telemetry: GPU busy %, power, load
#   scripts/debug-connect.sh llm                       # show live routing
#   scripts/debug-connect.sh llm-set agent.turn local:gpt-oss-120b high
#   scripts/debug-connect.sh load gpt-oss-120b
#   scripts/debug-connect.sh raw GET /api/debug/whoami
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"

usage() {
  sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
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

_call() { # METHOD PATH [JSON_BODY]
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS -X "$method" -H "Authorization: Bearer $KEY")
  [ -n "$body" ] && args+=(-H "Content-Type: application/json" -d "$body")
  curl "${args[@]}" "$BASE$path"
}

# Read the prompt/SQL text: remaining args if present, else stdin (for heredocs
# and pipes). Lets you paste multi-line prompts without shell-quoting hell.
_text_arg() { if [ "$#" -gt 0 ]; then printf '%s' "$*"; else cat; fi; }

cmd="${1:-help}"
[ "$#" -gt 0 ] && shift || true

case "$cmd" in
  whoami) _call GET /api/debug/whoami | _pp ;;

  complete)
    SYSTEM="" TASK="" STRENGTH="" MAXTOK="" SCHEMA=""
    while [ "${1:-}" != "" ]; do
      case "$1" in
        --system) SYSTEM="$2"; shift 2 ;;
        --task) TASK="$2"; shift 2 ;;
        --strength) STRENGTH="$2"; shift 2 ;;
        --max-tokens) MAXTOK="$2"; shift 2 ;;
        --json-schema) SCHEMA="$2"; shift 2 ;;  # a JSON Schema string
        --) shift; break ;;
        -*) echo "unknown flag: $1" >&2; exit 2 ;;
        *) break ;;
      esac
    done
    USERTEXT="$(_text_arg "$@")"
    body="$(SYSTEM="$SYSTEM" TASK="$TASK" STRENGTH="$STRENGTH" MAXTOK="$MAXTOK" \
            SCHEMA="$SCHEMA" USERTEXT="$USERTEXT" python3 - <<'PY'
import json, os
b = {"user_text": os.environ["USERTEXT"]}
for key, env in (("system","SYSTEM"),("task","TASK"),("strength","STRENGTH")):
    if os.environ.get(env): b[key] = os.environ[env]
if os.environ.get("MAXTOK"): b["max_tokens"] = int(os.environ["MAXTOK"])
if os.environ.get("SCHEMA"): b["json_schema"] = json.loads(os.environ["SCHEMA"])
print(json.dumps(b))
PY
)"
    _call POST /api/debug/complete "$body" | _pp
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

  raw) # METHOD PATH [JSON_BODY] — escape hatch for anything not wrapped above
    method="${1:?usage: debug-connect.sh raw <METHOD> <path> [body]}"
    path="${2:?usage: debug-connect.sh raw <METHOD> <path> [body]}"
    _call "$method" "$path" "${3:-}" | _pp
    ;;

  help|-h|--help) usage 0 ;;
  *) echo "unknown command: $cmd" >&2; usage 2 ;;
esac
