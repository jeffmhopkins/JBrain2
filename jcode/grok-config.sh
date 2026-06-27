# Point xAI's Grok Build CLI at the on-box coder model instead of xAI's cloud.
#
# Grok Build selects its model from ~/.grok/config.toml (a custom [model.X] with a
# base_url routes to any OpenAI-compatible endpoint; a non-default base_url makes the CLI
# send `Authorization: Bearer <env_key>` instead of xAI session auth). The compose service
# supplies the gateway URL / model / key as env; this hook renders them into the config on
# every login shell so a session's model pin (GROK_MODEL, set per session by the terminal)
# takes effect — the same reason `claude` gets ANTHROPIC_* pins. Sourced by `/bin/bash -l`
# via /etc/profile.d; a no-op if `grok` isn't installed.
#
# context_window MUST be set: for a custom (non-builtin) model Grok Build can't know the
# window and defaults to 200000, under-reporting the gateway's real size and triggering
# auto-compaction too early. Pin it to the served window (default 262144 = the coder's
# native 256k) so the meter is accurate and compaction fires at the true limit.
if command -v grok >/dev/null 2>&1; then
  mkdir -p "${HOME:-/root}/.grok"
  cat > "${HOME:-/root}/.grok/config.toml" <<TOML
[models]
default = "on-box-coder"

[model.on-box-coder]
model = "${GROK_MODEL:-qwen3-coder-next}"
base_url = "${GROK_MODELS_BASE_URL:-http://local-llm:8080/v1}"
name = "On-box coder"
env_key = "GROK_API_KEY"
context_window = ${GROK_CONTEXT_WINDOW:-262144}
TOML
fi
