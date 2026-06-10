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

# --- Python backend (FastAPI, pytest, ruff, pyright) ---
PYPROJECT=""
if [ -f backend/pyproject.toml ]; then
  PYPROJECT="backend"
elif [ -f pyproject.toml ]; then
  PYPROJECT="."
fi

if [ -n "$PYPROJECT" ]; then
  if ! command -v uv >/dev/null 2>&1; then
    log "installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi
  log "syncing Python dependencies in $PYPROJECT (uv sync --all-extras)"
  (cd "$PYPROJECT" && uv sync --all-extras)
else
  log "no pyproject.toml yet — skipping Python setup"
fi

# --- Frontend (React/Vite, vitest, biome) ---
if [ -f frontend/package.json ]; then
  log "installing frontend dependencies (npm install)"
  (cd frontend && npm install)
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
