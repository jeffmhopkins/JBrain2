#!/usr/bin/env bash
# Opt-in LLM-in-the-loop calibration for the wiki_lint (Wave B) verifier prompts
# (docs/archive/WIKI_LINT_PLAN.md §7-8). Drives the real contradiction / stale-claim
# prompts over labelled cases and scores precision/recall against the false-positive
# guard cases — run BEFORE enabling wiki_lint in prod or after a verifier prompt edit.
#
# The model is reached through the owner debug console (/api/debug/complete), so no raw
# provider key is needed — the deployment runs its own routed model. Supply EITHER:
#   HB_TOKEN=<base64 debug-console envelope>     (decoded here; never stored)
#   or HB_URL=<https://host> HB_KEY=<capability token>
# Optional: WIKI_LINT_EVAL_TASK (default wiki.ground — a deployed high-effort verifier
# task; set to wiki.lint.contradiction once Wave B is deployed and routed).
#
#   HB_TOKEN=… scripts/wiki-lint-eval.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../backend"
export PATH="$HOME/.local/bin:$PATH"

if [[ -n "${HB_TOKEN:-}" && ( -z "${HB_URL:-}" || -z "${HB_KEY:-}" ) ]]; then
  # Decode the {v,u,k} envelope into HB_URL/HB_KEY without echoing the key.
  eval "$(uv run python - "$HB_TOKEN" <<'PY'
import base64, json, sys
t = sys.argv[1]
d = json.loads(base64.b64decode(t + "==" * (-len(t) % 4)))
print(f'export HB_URL={d["u"]!r}')
print(f'export HB_KEY={d["k"]!r}')
PY
)"
fi

exec uv run python -m jbrain.evals.wiki_lint_runner "$@"
