#!/usr/bin/env bash
# Bootstraps a complete development environment from a fresh checkout.
#
# Single source of truth for dev tooling: any PR that adds a dependency or
# tool must update this script (see docs/DEVELOPMENT.md). Idempotent — it
# detects which parts of the project exist yet and skips the rest, so it
# stays valid at every phase of the roadmap.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

log() { printf '[dev-setup] %s\n' "$*"; }

# Skip a sync when the lockfile hasn't changed since the last successful run;
# stamp files make repeat sessions near-instant on a cached container.
STAMP_DIR="$ROOT/.dev-setup-stamps"
mkdir -p "$STAMP_DIR"

fresh() { # fresh <stamp-name> <lockfile> — 0 if stamp is current
  [ -f "$STAMP_DIR/$1" ] && [ -f "$2" ] && [ "$STAMP_DIR/$1" -nt "$2" ]
}

# --- Python (backend + supervisor: FastAPI, pytest, ruff, pyright) ---
ensure_uv() {
  if ! command -v uv >/dev/null 2>&1; then
    log "installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi
}

sync_python() { # sync_python <dir>
  local dir="$1" stamp="py-${1//\//-}"
  if [ ! -f "$dir/pyproject.toml" ]; then
    log "no $dir/pyproject.toml yet — skipping"
    return 0
  fi
  if fresh "$stamp" "$dir/uv.lock"; then
    log "$dir dependencies already current"
    return 0
  fi
  ensure_uv
  log "syncing $dir dependencies (uv sync --all-extras)"
  (cd "$dir" && uv sync --all-extras)
  touch "$STAMP_DIR/$stamp"
}

sync_python backend
sync_python supervisor

# --- Frontend (React/Vite, vitest, biome) ---
if [ -f frontend/package.json ]; then
  if fresh node frontend/package-lock.json; then
    log "frontend dependencies already current"
  else
    log "installing frontend dependencies (npm install)"
    (cd frontend && npm install)
    touch "$STAMP_DIR/node"
  fi
else
  log "no frontend/package.json yet — skipping Node setup"
fi

# --- Docker (testcontainers integration tests + the LLM-in-the-middle harness) ---
# Best-effort: managed environments start their own daemon. This sandbox does
# not, and its kernel has no usable bridge networking, so we start dockerd
# bridge-less and the test/harness code falls back to host networking
# (tests/conftest.py pgvector_container, scripts/llm-harness.sh). Never fatal:
# unit tests and linters don't need Docker.
HARNESS_IMAGE="timescale/timescaledb-ha:pg17"  # prod Postgres image, also used by the harness
SEARXNG_IMAGE="${SEARXNG_IMAGE:-docker.io/searxng/searxng:latest}"  # jerv web search (stock stack service)
MQTT_IMAGE="${MQTT_IMAGE:-iegomez/mosquitto-go-auth:latest}"  # opt-in JBrain360 broker (`mqtt` profile); pin by digest for deploy

if ! docker info >/dev/null 2>&1; then
  if command -v dockerd >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    log "starting dockerd (bridge-less — sandbox kernel has no bridge networking)"
    sudo dockerd --iptables=false --bridge=none >/tmp/dockerd.log 2>&1 &
    for _ in $(seq 1 15); do docker info >/dev/null 2>&1 && break; sleep 1; done
  fi
fi

if docker info >/dev/null 2>&1; then
  log "docker daemon available — integration tests and the LLM harness can run"
  # Pre-pull the harness/Postgres image so the first integration run isn't
  # racing a Docker Hub rate limit; best-effort, retried a few times.
  if ! docker image inspect "$HARNESS_IMAGE" >/dev/null 2>&1; then
    log "pre-pulling $HARNESS_IMAGE (harness + integration DB)"
    for _ in 1 2 3; do docker pull "$HARNESS_IMAGE" >/dev/null 2>&1 && break; sleep 10; done \
      || log "WARNING: could not pre-pull $HARNESS_IMAGE — it will pull on first use"
  fi
  # Pre-pull the SearXNG image (jerv web search) so a `jbrain up`/update isn't a
  # cold pull; best-effort. CI never starts the service (web search/fetch are faked
  # via MockTransport), so this is local/dev only.
  if ! docker image inspect "$SEARXNG_IMAGE" >/dev/null 2>&1; then
    log "pre-pulling $SEARXNG_IMAGE (jerv web search)"
    for _ in 1 2 3; do docker pull "$SEARXNG_IMAGE" >/dev/null 2>&1 && break; sleep 10; done \
      || log "WARNING: could not pre-pull $SEARXNG_IMAGE — it will pull when the stack starts"
  fi
  # Pre-pull the opt-in MQTT broker image (JBrain360 M0, `mqtt` profile) so the
  # secure spine isn't a cold pull; best-effort. CI never runs the profile (the
  # auth/ACL endpoints are tested directly), so this is local/dev convenience only.
  if ! docker image inspect "$MQTT_IMAGE" >/dev/null 2>&1; then
    log "pre-pulling $MQTT_IMAGE (opt-in mqtt profile)"
    for _ in 1 2 3; do docker pull "$MQTT_IMAGE" >/dev/null 2>&1 && break; sleep 10; done \
      || log "WARNING: could not pre-pull $MQTT_IMAGE — it will pull when the profile is enabled"
  fi
else
  log "WARNING: no docker daemon — testcontainers integration tests and the LLM" \
      "harness will be skipped; unit tests and linters are unaffected"
fi

# --- Android app build (opt-in, NOT bootstrapped here) ---
# The JBrain360 app (android/) builds against the Android SDK — ~1 GB of
# downloads a web/CI container never needs — so it is provisioned separately by
# android/setup-android-sdk.sh, and CI's `android` job sets up its own SDK.
# Mentioned here per the dev-setup single-source-of-truth rule; a no-op in dev.

# --- Local-network access / mDNS (production host only, NOT bootstrapped here) ---
# LAN access (docs/LOCAL_ACCESS.md, on by default) installs avahi-daemon +
# python3-dbus + python3-gi on the deploy host (deploy/lan-setup.sh) so the box
# answers as jbrain.local via a CNAME alias; Caddy serves local HTTPS via its
# internal CA. That is a production-host concern with no dev equivalent — the
# proxy entrypoint renders the LAN site from JBRAIN_LAN_ADDR at container start.
# Mentioned here per the dev-setup single-source-of-truth rule; a no-op in dev.

# --- Local model hosting (opt-in, NOT bootstrapped here) ---
# Self-hosted models (Settings → LLM, AMD Strix Halo class box) are provisioned
# separately by scripts/local-llm-setup.sh: it downloads tens of GB of weights
# and starts a GPU service, so it must NEVER run from this auto-bootstrapped
# path (web/CI containers have no GPU). Mentioned here per the dev-setup
# single-source-of-truth rule; deliberately a no-op in dev.

# --- Image generation / ComfyUI (opt-in, HOST-managed, NOT bootstrapped here) ---
# jerv's generate_image/edit_image tools drive a host-managed ComfyUI running
# Qwen-Image-2512 / Qwen-Image-Edit on a gfx1151 box (docs/STRIX_HALO_SETUP.md,
# "Image generation"). Like the local LLM gateway it is HOST-MANAGED: JBrain2 does
# NOT containerize ComfyUI and adds NO new backend dependency for it — the backend
# only POSTs a workflow graph over HTTP. Point the app at it with the env var
#   JBRAIN_COMFYUI_URL=http://127.0.0.1:8188   # the box's localhost ComfyUI
# Empty (the default) disables the feature and hides both tools — so this is a
# no-op in dev/CI (no GPU). Mentioned here per the dev-setup single-source-of-truth
# rule (CLAUDE.md rule #8).

log "done"
