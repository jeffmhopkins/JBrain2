#!/bin/sh
# Exactly one on-box LLM engine, on every path that starts one.
#
# A LIBRARY, sourced (`. src/deploy/local-engine.sh`) by the scripts that bring a gateway up:
# deploy/update-inner.sh, deploy/local-models-sync.sh and scripts/local-llm-setup.sh. It
# defines functions and runs nothing.
#
# Two engines exist (docs/plans/FLASH_NEXT_ENGINE_PLAN.md): the standard `local-llm` gateway
# and the `flash-next` container. Both up at once is ~170 GiB on a 128 GB box — a freeze, the
# failure this repo has already paid for in hard-locked updates. Each of those scripts used to
# say `up -d local-llm` on its own; three copies of "which one, and stop the other" would
# drift, so the decision lives here once (§4d).
#
# The selected engine is an owner SETTING, read through the api image's CLI — never an `.env`
# flag the owner would have to edit from a shell they do not have (CLAUDE.md #10). An
# unreadable setting reads as `standard`, the engine every box has.
#
# POSIX sh: it runs inside the bash-less docker:cli updater. Every caller runs from the
# install dir (docker-compose.yml + .env + ./src).

# shellcheck disable=SC2034  # LOCAL_ENGINE_STARTED is read by the scripts that source this.

local_engine_say() { printf '[local-engine] %s\n' "$*"; }

# Optional command prefix for the catalog read below — update-inner.sh sets it to its bounded
# runner (`run_bounded <seconds>`), because that read happens with the stack quiesced, where a
# `compose run` that never starts would hang the whole update. Empty everywhere else.
LOCAL_ENGINE_RUNNER="${LOCAL_ENGINE_RUNNER:-}"

# The service each engine runs as (jbrain.llm.engine.SERVICE). The compose PROFILE carries
# the same name, which is why one variable serves both.
local_engine_service() {
  case "$1" in
    flash-next) echo flash-next ;;
    *) echo local-llm ;;
  esac
}

# One engine name out of whatever the CLI printed: the last non-blank line, so compose's own
# chatter or a TIMEOUT line from a bounded runner cannot be mistaken for a value, and
# anything unrecognised (an old api image without the subcommand, a DB blip) is `standard`.
local_engine_parse() {
  _le_last="$(printf '%s\n' "${1:-}" | tr -d '\r' | sed '/^[[:space:]]*$/d' | tail -n1 | tr -d ' ')"
  case "$_le_last" in
    flash-next) echo flash-next ;;
    *) echo standard ;;
  esac
}

# The selected engine, read from the settings store. Unbounded: callers that need a ceiling
# (update-inner.sh) run the same CLI through their own bounded runner and call
# local_engine_parse on its output.
local_engine_read() {
  local_engine_parse "$(docker compose run --rm --no-deps -T api python -m jbrain.cli local-engine 2>/dev/null || true)"
}

# The ids LOCAL_MODELS (.env) says are installed, space-separated. That line is written only
# by automation (the model sync), so it is the record of what has weights on disk.
local_engine_installed_ids() {
  grep '^LOCAL_MODELS=' .env 2>/dev/null | tail -n1 | sed 's/^LOCAL_MODELS=//' \
    | tr -d '[]" ' | tr ',' ' ' || true
}

# Succeeds when any id in $1 (a whitespace list) is served by the Flash-Next engine, by the
# catalog's own `engine` field — so a later Flash-Next variant needs no edit here. An id the
# catalog does not know counts as standard.
local_engine_any_flash_next() {
  [ -n "$(printf '%s' "${1:-}" | tr -d '[:space:]')" ] || return 1
  # shellcheck disable=SC2086  # the runner is a deliberately word-split command prefix.
  _le_out="$($LOCAL_ENGINE_RUNNER docker compose run --rm --no-deps -T -e IDS="$1" api python -c '
import os
from jbrain.llm import local_catalog
ids = os.environ["IDS"].split()
print("yes" if any(getattr(local_catalog.get(i), "engine", "standard") == "flash-next" for i in ids) else "no")
' 2>/dev/null || true)"
  [ "$(printf '%s\n' "$_le_out" | tr -d '\r' | sed '/^[[:space:]]*$/d' | tail -n1)" = yes ]
}

# "1" when a Flash-Next model is installed per LOCAL_MODELS, else empty. A value rather than
# a status so it can be captured once and passed to local_engine_start.
local_engine_flash_next_installed() {
  if local_engine_any_flash_next "$(local_engine_installed_ids)"; then echo 1; fi
}

# Bring up exactly ONE engine.
#
#   $1  the selected engine (standard | flash-next)
#   $2  "1" when Flash-Next is installed, else empty
#
# The other engine is STOPPED FIRST — before anything starts — and then left CREATED but not
# running (`up --no-start`): the supervisor's /start only starts a container that exists (404
# otherwise), and every update removes both, so without this a switch made after an update
# would have nothing to start (§4d "Provisioning"). Flash-Next is created only when installed:
# a box that never provisions it never gets its container or its image built.
#
# Flash-Next selected but not installed, or failing to start, falls back to the standard
# gateway and SAYS SO — the owner reads this in the PWA's update log, and a box with no
# engine at all is worse than one on the other engine. The setting itself is left alone:
# rewriting the owner's choice is the switch's job (F3), not a deploy script's.
#
# Sets LOCAL_ENGINE_STARTED to the engine actually started.
local_engine_start() {
  _le_engine="${1:-standard}"
  _le_fn="${2:-}"
  LOCAL_ENGINE_STARTED=''
  if [ "$_le_engine" = flash-next ] && [ "$_le_fn" != 1 ]; then
    local_engine_say "Flash-Next is the selected engine but its weights are not installed — starting the standard engine instead (install it from Settings -> On-box models)"
    _le_engine=standard
  fi
  if [ "$_le_engine" = flash-next ]; then
    docker compose --profile local-llm stop local-llm >/dev/null 2>&1 || true
    docker compose --profile local-llm up --no-start local-llm >/dev/null 2>&1 \
      || local_engine_say "WARNING: could not create the standard gateway (stopped) — switching back will need an Update first"
    if docker compose --profile flash-next up -d flash-next; then
      LOCAL_ENGINE_STARTED=flash-next
      local_engine_say "Flash-Next engine started; the standard gateway is created and stopped"
      return 0
    fi
    local_engine_say "WARNING: the Flash-Next engine did not start — falling back to the standard engine"
  fi
  docker compose --profile flash-next stop flash-next >/dev/null 2>&1 || true
  if [ "$_le_fn" = 1 ]; then
    docker compose --profile flash-next up --no-start flash-next >/dev/null 2>&1 \
      || local_engine_say "WARNING: could not create the Flash-Next container (stopped)"
  fi
  LOCAL_ENGINE_STARTED=standard
  docker compose --profile local-llm up -d local-llm
}
