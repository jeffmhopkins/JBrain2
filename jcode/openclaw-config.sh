# Point OpenClaw (openclaw/openclaw) at the on-box coder model instead of a cloud provider.
#
# OpenClaw reads ~/.openclaw/openclaw.json. A custom provider under models.providers with
# `api: "openai-completions"` routes to any OpenAI-compatible endpoint; agents.defaults.model
# .primary ("<provider>/<model-id>") selects it as the default. The compose service supplies
# the gateway URL / model / key as env; this hook renders them into the config on every login
# shell so a session's model pin (OPENCLAW_MODEL, set per session by the terminal) takes
# effect — the same reason `claude` gets ANTHROPIC_* pins and `grok` gets GROK_*. Sourced by
# `/bin/bash -l` via /etc/profile.d; a no-op if `openclaw` isn't installed.
#
# The gateway is keyless, but a custom (non-native) endpoint still sends Authorization:
# Bearer, so OPENCLAW_API_KEY is a non-empty placeholder. contextWindow MUST be set: for a
# custom model OpenClaw can't know the window, and an accurate value keeps its token meter and
# auto-compaction honest — pin it to the served window (default 262144 = the coder's native
# 256k). We deliberately do NOT set an agents.defaults.models allowlist: when unset every
# provider model is allowed, which avoids the "model not allowed" error on a single-model box.
#
# The `gateway` block is required to USE openclaw (not just validate config): OpenClaw's
# gateway "refuses to start unless gateway.mode=local is set". This ONLY makes the local
# gateway permitted — it does not start it; `jcode-openclaw gateway` does that on demand
# (a persistent daemon isn't worth running in every ephemeral session). Its loopback port
# defaults to 18789; OPENCLAW_GATEWAY_PORT overrides it (e.g. a per-session port if concurrent
# sessions ever each run a gateway — sessions share the container's loopback).
if command -v openclaw >/dev/null 2>&1; then
  mkdir -p "${HOME:-/root}/.openclaw"
  cat > "${HOME:-/root}/.openclaw/openclaw.json" <<JSON
{
  "agents": {
    "defaults": {
      "model": { "primary": "on-box-coder/${OPENCLAW_MODEL:-qwen3-coder-next}" }
    }
  },
  "gateway": {
    "mode": "local",
    "port": ${OPENCLAW_GATEWAY_PORT:-18789}
  },
  "models": {
    "providers": {
      "on-box-coder": {
        "baseUrl": "${OPENCLAW_MODELS_BASE_URL:-http://local-llm:8080/v1}",
        "apiKey": "${OPENCLAW_API_KEY:-sk-local-noauth}",
        "api": "openai-completions",
        "timeoutSeconds": 300,
        "models": [
          {
            "id": "${OPENCLAW_MODEL:-qwen3-coder-next}",
            "name": "On-box coder",
            "reasoning": false,
            "input": ["text"],
            "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 },
            "contextWindow": ${OPENCLAW_CONTEXT_WINDOW:-262144},
            "maxTokens": 8192
          }
        ]
      }
    }
  }
}
JSON
fi
