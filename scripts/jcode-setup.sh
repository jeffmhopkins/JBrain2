#!/usr/bin/env bash
# OPT-IN: enable code mode (jcode) — a sandboxed coding-session sidecar running
# Claude Code's agent engine against an ON-BOX coder model (docs/proposed/JCODE_PLAN.md).
#
#   sudo bash scripts/jcode-setup.sh
#
# This is NEVER run by the default install or by dev-setup.sh. It:
#   1. mints a JCODE_TOKEN (the api↔jcode shared bearer) if one isn't set,
#   2. writes the JCODE_* / JBRAIN_JCODE_* keys into .env (fail-closed defaults),
#   3. builds the jcode image and starts the `jcode` compose profile.
#
# The coder model is LOCAL: jcode points the Agent SDK at the local-llm gateway's
# Anthropic-compatible endpoint, so the `local-llm` profile must also be enabled
# (scripts/local-llm-setup.sh) with a coder model provisioned (Qwen3-Coder-Next).
# Nothing leaves the box. Run from the install dir (/opt/jbrain2).
set -euo pipefail

INSTALL_DIR="${JBRAIN_INSTALL_DIR:-/opt/jbrain2}"
cd "$INSTALL_DIR"

say() { printf '\n[jcode] %s\n' "$*"; }

[ -f .env ] || { echo "No .env in $INSTALL_DIR — run deploy/install.sh first." >&2; exit 1; }

# Idempotent .env upsert: replace KEY=... in place, or append it.
set_env() { # set_env KEY VALUE
  local key="$1" val="$2"
  if grep -q "^${key}=" .env; then
    sed -i "s|^${key}=.*|${key}=${val}|" .env
  else
    printf '%s=%s\n' "$key" "$val" >> .env
  fi
}

# Mint a token only if one isn't already present (don't rotate on a re-run — that
# would orphan a running api's credential until its next recreate).
if ! grep -q '^JCODE_TOKEN=.\+' .env; then
  TOKEN="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  set_env JCODE_TOKEN "$TOKEN"
  say "minted a new JCODE_TOKEN"
fi

set_env JCODE_ENABLED true
set_env JCODE_URL "http://jcode:9100"
set_env JCODE_MODEL "${JCODE_MODEL:-qwen3-coder-next}"
set_env JCODE_MODEL_URL "${JCODE_MODEL_URL:-http://local-llm:8080}"

if ! grep -q '^LOCAL_LLM_ENABLED=true' .env; then
  say "WARNING: the local-llm gateway isn't enabled — jcode has no model to talk to."
  say "         run scripts/local-llm-setup.sh and provision a coder model first."
fi

say "building the jcode image and starting the profile"
docker compose --profile jcode build jcode
docker compose --profile jcode up -d jcode

# The api must be recreated to pick up the new JBRAIN_JCODE_* env (a restart reuses
# the old environment) — same caveat as the debug-access flag.
say "recreating the api to pick up JBRAIN_JCODE_* (Wave J2 routes)"
docker compose up -d api

say "done — code mode enabled. ON-BOX VERIFICATION still required (JCODE_PLAN.md"
say "open decision 1): confirm the gateway serves an Anthropic /v1/messages endpoint"
say "(or add a shim) and that a coding turn drives Qwen3-Coder-Next end to end."
say ""
say "This is a ONE-TIME enable. From now on the normal update (the PWA's Update"
say "button or 'jbrain update') rebuilds and recreates jcode automatically and"
say "backfills any missing JCODE_* keys — no need to re-run this script."
say ""
say "Web preview (Wave J4) is OFF by default — it opens an ephemeral Cloudflare tunnel"
say "to the sandbox's dev server, reachable by anyone with the (random) URL. To enable:"
say "set JCODE_PREVIEW_ENABLED=true in .env and re-run this script (or 'jbrain up jcode')."
