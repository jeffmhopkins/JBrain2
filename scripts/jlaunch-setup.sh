#!/usr/bin/env bash
# OPT-IN: enable the job launcher (jlaunch) — a sidecar that runs a registered
# long-running one-shot computation (the Erdos-Straus census is the first spec) as a
# supervised job with a live terminal, start/stop/kill, and a public, shareable results
# page + artifact (docs/plans/JLAUNCH_PLAN.md).
#
#   sudo bash scripts/jlaunch-setup.sh
#
# This is NEVER run by the default install or by dev-setup.sh. It:
#   1. mints a JLAUNCH_TOKEN (the api<->jlaunch shared bearer) if one isn't set,
#   2. writes the JLAUNCH_* keys into .env (fail-closed defaults),
#   3. builds the jlaunch image and starts the `jlaunch` compose profile,
#   4. recreates the api to pick up the new JBRAIN_JLAUNCH_* env.
#
# Unlike jcode, jlaunch needs no on-box model — a job builds its own venv and runs a
# shell pipeline. It DOES need outbound NAT (it git-clones the compute repo and pip-installs
# from PyPI). A run is meant to saturate the box for hours (uncapped cpu/mem), so expect
# chat/code-mode/image-gen to slow while one is active. Run from the install dir.
set -euo pipefail

INSTALL_DIR="${JBRAIN_INSTALL_DIR:-/opt/jbrain2}"
cd "$INSTALL_DIR"

say() { printf '\n[jlaunch] %s\n' "$*"; }

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

# Mint a token only if one isn't already present (don't rotate on a re-run — that would
# orphan a running api's credential until its next recreate).
if ! grep -q '^JLAUNCH_TOKEN=.\+' .env; then
  TOKEN="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  set_env JLAUNCH_TOKEN "$TOKEN"
  say "minted a new JLAUNCH_TOKEN"
fi

set_env JLAUNCH_ENABLED true
set_env JLAUNCH_URL "http://jlaunch:9101"

say "building the jlaunch image and starting the profile"
docker compose --profile jlaunch build jlaunch
docker compose --profile jlaunch up -d jlaunch

# The api must be recreated to pick up the new JBRAIN_JLAUNCH_* env (a restart reuses the
# old environment) — same caveat as the jcode/debug-access flags.
say "recreating the api to pick up JBRAIN_JLAUNCH_* (the /jlaunch routes + launcher tile)"
docker compose up -d api

say "done — the job launcher is enabled."
say ""
say "Open the launcher in the PWA — a 'Math' tile appears. Start the Erdos-Straus census,"
say "watch the live terminal, stop/kill if needed, and once it finishes generate a public"
say "results sharelink (headline block + machine specs + artifact download)."
say ""
say "This is a ONE-TIME enable. From now on the normal update (the PWA's Update button or"
say "'jbrain update') rebuilds and recreates jlaunch automatically and backfills any missing"
say "JLAUNCH_* keys — no need to re-run this script."
