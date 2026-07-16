# Point xAI's Grok Build CLI at the on-box models — with EVERY installed tool-capable
# model as a switchable entry, so `/model` flips between them live (plan on the reasoner,
# execute on the coder) within one session.
#
# The model list is fetched from the api's residency-aware jcode proxy
# (GROK_MODELS_BASE_URL): that proxy evicts-to-budget before each completion, so a
# plan↔execute switch COLD-SWAPS safely instead of stacking two large models and freezing
# the unified-memory box. Grok reaches the same base_url for its completions, so every
# request — and every switch — goes through the budget. Rendered on every login shell so a
# session's GROK_MODEL default (set per session by the terminal) and the current installed
# set both take effect.
#
# Each block's TABLE KEY is a short alias (proxy field 1: `oss`, `qwen`, …) so `/model oss`
# is enough — the block's `model =` carries the real served name the proxy validates and
# forwards. The key is QUOTED (aliases/served names can contain dots, which a bare TOML key
# would parse as nested tables). A non-default base_url makes the CLI send `Authorization:
# Bearer $GROK_API_KEY` (the shared jcode token the proxy checks). context_window MUST be set
# per model: a custom model defaults to 200000, under-reporting the served window and
# auto-compacting early — so each block pins the model's own window. If the list can't be
# fetched (api briefly unreachable, or no curl), it falls back to a single-model config for
# the session's pinned GROK_MODEL, so a shell still works.
if command -v grok >/dev/null 2>&1; then
  mkdir -p "${HOME:-/root}/.grok"
  base_url="${GROK_MODELS_BASE_URL:-http://local-llm:8080/v1}"
  default_model="${GROK_MODEL:-qwen3-coder-next}"
  # Subagents are grok Build's task agents. They're ON — but grok has no sequential/
  # concurrency knob (verified against grok-build source: only `enabled` + per-type
  # `toggle`, no max-parallel), so it's the api proxy's swap lock that keeps them from
  # THRASHING: every completion serializes there, so on this can't-co-reside box the
  # subagents queue one model at a time instead of running in parallel across models.
  # JCODE_GROK_SUBAGENTS=false disables them entirely.
  subagents_enabled="${JCODE_GROK_SUBAGENTS:-true}"
  # Route grok's built-in `plan` subagent to a planner model (plan on the reasoner, execute
  # on the coder) — but only when JCODE_GROK_PLAN_MODEL names an INSTALLED served model (grok
  # requires it exist in its registry). The api sets it per session from the owner's planner
  # selection; EMPTY means single-model (the executor plans too), so the plan pin is omitted.
  # No hardcoded default here — the split default lives in the api (Settings.jcode_planner_model),
  # so this stays a pure mechanism.
  plan_model="${JCODE_GROK_PLAN_MODEL:-}"
  models_lines=""
  if command -v curl >/dev/null 2>&1; then
    models_lines="$(curl -fsS -H "Authorization: Bearer ${GROK_API_KEY:-}" \
      "${base_url}/models?format=lines" 2>/dev/null || true)"
  fi
  # Map the session default + planner (both served names) to their aliases — the config keys
  # `[models] default` and `[subagents.models] plan` reference the block key, not the served
  # name. A `|served|`-delimited match picks the line; field 1 is its alias. Empty plan_alias
  # (planner not installed) simply omits the plan pin.
  default_alias="$default_model"
  a="$(printf '%s\n' "$models_lines" | grep -F "|${default_model}|" | head -1 | cut -d'|' -f1)"
  [ -n "$a" ] && default_alias="$a"
  plan_alias="$(printf '%s\n' "$models_lines" | grep -F "|${plan_model}|" | head -1 | cut -d'|' -f1)"
  {
    echo "[models]"
    echo "default = \"${default_alias}\""
    if [ -n "$models_lines" ]; then
      # One block per installed model: alias|served|label|window per line from the proxy.
      printf '%s\n' "$models_lines" | while IFS='|' read -r alias served label window; do
        [ -n "$alias" ] || continue
        printf '\n[model."%s"]\n' "$alias"
        echo "model = \"${served}\""
        echo "base_url = \"${base_url}\""
        echo "name = \"${label:-$served}\""
        echo "env_key = \"GROK_API_KEY\""
        echo "context_window = ${window:-262144}"
      done
    else
      # Fallback: just the session's pinned model (list unavailable — api down / no curl).
      printf '\n[model."%s"]\n' "$default_alias"
      echo "model = \"${default_model}\""
      echo "base_url = \"${base_url}\""
      echo "name = \"On-box coder\""
      echo "env_key = \"GROK_API_KEY\""
      echo "context_window = ${GROK_CONTEXT_WINDOW:-262144}"
    fi
    # Subagents (top-level table, after the model blocks). Enabled; the proxy's swap lock
    # serializes their model execution so they never run in parallel across models.
    printf '\n[subagents]\n'
    echo "enabled = ${subagents_enabled}"
    # Bind the plan subagent to the reasoner by its alias — only when it's on and that model
    # is installed (plan_alias non-empty), else grok would reject a missing-model pin.
    if [ "$subagents_enabled" = true ] && [ -n "$plan_alias" ]; then
      printf '\n[subagents.models]\n'
      echo "plan = \"${plan_alias}\""
    fi
  } > "${HOME:-/root}/.grok/config.toml"
fi
