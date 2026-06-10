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

# --- Docker (testcontainers-based integration tests) ---
if docker info >/dev/null 2>&1; then
  log "docker daemon available — integration tests can run"
else
  log "WARNING: no docker daemon — testcontainers integration tests will be" \
      "skipped; unit tests and linters are unaffected"
fi

log "done"
