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
else
  log "WARNING: no docker daemon — testcontainers integration tests and the LLM" \
      "harness will be skipped; unit tests and linters are unaffected"
fi

log "done"
