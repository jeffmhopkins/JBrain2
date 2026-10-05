#!/usr/bin/env bash
# The browse benchmark (docs/plans/BROWSER_FAST_LOOP_PLAN.md L0): six generic tasks through
# the debug console's /browse, one line each — so a wave's numbers are re-measured the same
# way every time, from a Claude session, with no terminal on the box.
#
#   scripts/browse-bench.sh <label> [--budget N] [--loop fast|b1] [--only NAME]
#
# Per task it prints: the label, the task, the outcome, whether the host's fact check
# verified the answer, wall seconds, steps, and milliseconds per phase — navigation (every
# step that is not the end), the finish decision (`finish` / `done`), and the extraction
# (every `extract*` step). Runs go one after another: the box browses one goal at a time.
# Needs a debug token like scripts/debug-connect.sh (it calls that script's `browse`).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONNECT="${BROWSE_BENCH_CONNECT:-$HERE/debug-connect.sh}"

usage() {
  echo "usage: browse-bench.sh <label> [--budget N] [--loop fast|b1] [--only NAME]" >&2
  exit 2
}

LABEL="${1:-}"
[ -n "$LABEL" ] || usage
case "$LABEL" in -*) usage ;; esac
shift
BUDGET="" LOOP="" ONLY=""
while [ "${1:-}" != "" ]; do
  case "$1" in
    --budget) BUDGET="${2:-}"; [ -n "$BUDGET" ] || usage; shift 2 ;;
    --loop) LOOP="${2:-}"; [ -n "$LOOP" ] || usage; shift 2 ;;
    --only) ONLY="${2:-}"; [ -n "$ONLY" ] || usage; shift 2 ;;
    *) echo "unknown flag: $1" >&2; usage ;;
  esac
done

# name | start URL | goal. Generic shapes, not tuned sites: a location picker, a site search
# box, a paginated list, a tab, one filter, a store locator.
TASKS=(
  "cinema|https://www.epictheatres.com/|On epictheatres.com pick the Titusville theater and list today's showtimes (film and times)."
  "search|https://en.wikipedia.org/|On en.wikipedia.org search for Kennedy Space Center and report the year it was established."
  "paginated|https://news.ycombinator.com/|On news.ycombinator.com go to page 2 of the front page and list the first three story titles."
  "tab|https://github.com/microsoft/playwright-mcp|On github.com/microsoft/playwright-mcp find the latest release and report its version and date."
  "filter|https://books.toscrape.com/|On books.toscrape.com open the Travel category and report how many books it has and the first title."
  "locator|https://stores.barnesandnoble.com/|On stores.barnesandnoble.com find the Barnes & Noble store in Melbourne, FL and report its opening hours."
)

printf '%s\n' "label	task	outcome	verified	seconds	steps	nav_ms	finish_ms	extract_ms"
for row in "${TASKS[@]}"; do
  IFS='|' read -r name start goal <<<"$row"
  [ -z "$ONLY" ] || [ "$ONLY" = "$name" ] || continue
  args=(browse "$goal" --start-url "$start")
  [ -z "$BUDGET" ] || args+=(--budget "$BUDGET")
  [ -z "$LOOP" ] || args+=(--loop "$LOOP")
  out="$(bash "$CONNECT" "${args[@]}" 2>/dev/null)" || out=""
  LABEL="$LABEL" NAME="$name" OUT="$out" python3 - <<'PY'
import json, os

label, name = os.environ["LABEL"], os.environ["NAME"]
try:
    run = json.loads(os.environ["OUT"])["result"] or {}
except (ValueError, KeyError, TypeError):
    print(f"{label}\t{name}\terror\t-\t-\t-\t-\t-\t-")
    raise SystemExit(0)
nav = finish = extract = 0
for step in run.get("steps", []):
    action = step.get("action", "")
    if action.startswith("extract"):
        extract += step.get("model_ms", 0)
    elif action in ("finish", "done"):
        finish += step.get("model_ms", 0)
    else:
        nav += step.get("model_ms", 0) + step.get("browser_ms", 0)
print(
    f"{label}\t{name}\t{run.get('outcome', '?')}\t{'yes' if run.get('verified') else 'no'}"
    f"\t{run.get('elapsed_ms', 0) / 1000:.0f}\t{len(run.get('steps', []))}"
    f"\t{nav}\t{finish}\t{extract}"
)
PY
done
