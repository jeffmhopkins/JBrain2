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
# Each [model."X"] routes to that OpenAI-compatible endpoint; a non-default base_url makes
# the CLI send `Authorization: Bearer $GROK_API_KEY` (the shared jcode token the proxy
# checks). The table key is QUOTED because served names contain dots (glm-4.5-air), which a
# bare TOML key would parse as nested tables. context_window MUST be set per model: a custom
# model defaults to 200000, under-reporting the served window and auto-compacting early — so
# each block pins the model's own window. If the list can't be fetched (api briefly
# unreachable, or no curl), it falls back to a single-model config for the session's pinned
# GROK_MODEL, so a shell still works.
if command -v grok >/dev/null 2>&1; then
  mkdir -p "${HOME:-/root}/.grok"
  base_url="${GROK_MODELS_BASE_URL:-http://local-llm:8080/v1}"
  default_model="${GROK_MODEL:-qwen3-coder-next}"
  # Subagents are grok Build's parallelism (they run concurrently). On this box the two
  # large models can't co-reside, so parallel subagents on different models would cold-swap
  # against each other — the proxy's swap lock keeps that SAFE, but it's slow, so we default
  # them OFF (single model at a time; plan on the reasoner then execute on the coder via
  # `/model`). Set JCODE_GROK_SUBAGENTS=true to re-enable them (then a `[subagents.models]
  # plan = "gpt-oss-120b"` planner routing becomes possible, at the cold-swap cost).
  subagents_enabled="${JCODE_GROK_SUBAGENTS:-false}"
  models_lines=""
  if command -v curl >/dev/null 2>&1; then
    models_lines="$(curl -fsS -H "Authorization: Bearer ${GROK_API_KEY:-}" \
      "${base_url}/models?format=lines" 2>/dev/null || true)"
  fi
  {
    echo "[models]"
    echo "default = \"${default_model}\""
    if [ -n "$models_lines" ]; then
      # One block per installed model (served|label|window per line from the proxy).
      printf '%s\n' "$models_lines" | while IFS='|' read -r served label window; do
        [ -n "$served" ] || continue
        printf '\n[model."%s"]\n' "$served"
        echo "model = \"${served}\""
        echo "base_url = \"${base_url}\""
        echo "name = \"${label:-$served}\""
        echo "env_key = \"GROK_API_KEY\""
        echo "context_window = ${window:-262144}"
      done
    else
      # Fallback: just the session's pinned model (list unavailable — api down / no curl).
      printf '\n[model."%s"]\n' "$default_model"
      echo "model = \"${default_model}\""
      echo "base_url = \"${base_url}\""
      echo "name = \"On-box coder\""
      echo "env_key = \"GROK_API_KEY\""
      echo "context_window = ${GROK_CONTEXT_WINDOW:-262144}"
    fi
    # Parallelism control (top-level table, after the model blocks): off by default so one
    # model serves at a time — matches the single-model box and avoids cold-swap ping-pong.
    printf '\n[subagents]\n'
    echo "enabled = ${subagents_enabled}"
  } > "${HOME:-/root}/.grok/config.toml"
fi
