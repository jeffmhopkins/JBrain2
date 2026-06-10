#!/usr/bin/env bash
# Stand up a throwaway Postgres for the LLM-in-the-middle harness and drive it
# interactively (read the real note.extract prompt as the model; run a scenario
# file against the real pipeline). The committed golden scenarios run as part
# of the normal suite via `pytest -m integration tests/integration/test_harness_scenarios.py`;
# this script is for ad-hoc "be the model" exploration on a standing DB.
#
#   scripts/llm-harness.sh up          start DB + migrate
#   scripts/llm-harness.sh prompt      print the assembled system+user prompt
#   scripts/llm-harness.sh run FILE    run one scenario JSON, print + assert
#   scripts/llm-harness.sh down        remove the DB
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
IMG=timescale/timescaledb-ha:pg17
NAME=jbrain-harness-db
PORT=5544
SUPER_PW=harnesssuper
APP_PW=harnessapp
APP_URL="postgresql+asyncpg://jbrain_app:${APP_PW}@localhost:${PORT}/jbrain"

# The sandbox's dockerd runs bridge-less, so we use host networking and a
# non-default PGPORT rather than published ports (same reason as tests/conftest).
up() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run -d --name "$NAME" --network host \
    -e POSTGRES_DB=jbrain -e POSTGRES_USER=jbrain -e POSTGRES_PASSWORD="$SUPER_PW" \
    -e APP_DB_PASSWORD="$APP_PW" -e PGPORT="$PORT" \
    -v "$REPO/deploy/db-init:/docker-entrypoint-initdb.d:ro" \
    "$IMG" >/dev/null
  until docker exec "$NAME" pg_isready -p "$PORT" -U jbrain >/dev/null 2>&1; do sleep 2; done
  sleep 3  # init scripts (the app role) run after the first ready flap
  cd "$REPO/backend"
  JBRAIN_MIGRATION_DATABASE_URL="postgresql+asyncpg://jbrain:${SUPER_PW}@localhost:${PORT}/jbrain" \
    uv run alembic upgrade head
  echo "harness DB up on :${PORT}"
}

case "${1:-}" in
  up) up ;;
  down) docker rm -f "$NAME" >/dev/null 2>&1 || true; echo "harness DB removed" ;;
  prompt) cd "$REPO/backend" && uv run python -m tests.harness.runner prompt ;;
  run)
    [ -n "${2:-}" ] || { echo "usage: llm-harness.sh run <scenario.json>" >&2; exit 1; }
    cd "$REPO/backend"
    JBRAIN_DATABASE_URL="$APP_URL" uv run python -m tests.harness.runner run "$2"
    ;;
  *)
    echo "usage: llm-harness.sh {up|down|prompt|run FILE}" >&2
    exit 1
    ;;
esac
