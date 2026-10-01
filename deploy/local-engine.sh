#!/bin/sh
# Exactly one on-box LLM engine, on every path that starts one.
#
# A LIBRARY, sourced (`. src/deploy/local-engine.sh`) by the scripts that bring a gateway up:
# deploy/update-inner.sh, deploy/local-models-sync.sh, scripts/local-llm-setup.sh, and the
# one-service deploys deploy/refresh-inner.sh / deploy/rebuild-inner.sh for an engine. It
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
# DESIRED vs EFFECTIVE. That setting (`llm_local_engine`) is what the owner WANTS; it is never
# rewritten here. Whatever actually starts an engine records it as the EFFECTIVE engine
# (`llm_local_engine_effective`, local_engine_set_effective), and the api routes, lists and
# re-stamps by that — so a Flash-Next that could not start (no image, weights incomplete, a
# failed start) falls back to standard WITHOUT leaving the api refusing every standard load
# while it still believes Flash-Next is up. The next update tries the desired engine again.
#
# Every `up` carries --no-build: an implicit compose build of the Flash-Next image is a full
# llama.cpp compile, and the model sync runs these helpers with the stack up and serving,
# unbounded. An engine image is built in exactly two places, each bounded and each with NO
# engine running: update-inner.sh's quiesced window (local_engine_select) and an engine
# refresh/rebuild (local_engine_rebuild). An engine with no image is treated as absent.
#
# POSIX sh: it runs inside the bash-less docker:cli updater. Every caller runs from the
# install dir (docker-compose.yml + .env + ./src).

# shellcheck disable=SC2034  # ENGINE, SELECTED_ENGINE, FLASH_NEXT_READY, LOCAL_ENGINE_STARTED are read by the scripts that source this.

local_engine_say() { printf '[local-engine] %s\n' "$*"; }

# Optional command prefixes, each a deliberately word-split command (`run_bounded <seconds>`).
# update-inner.sh sets them to its bounded runner, because it calls these with the stack
# quiesced, where a `compose run` that never starts would hang the whole update.
#   RUNNER         short reads (settings, catalog)
#   UNLOAD_RUNNER  the model release before a stop — walks every resident model, so longer
#   BUILD_RUNNER   the Flash-Next image build
# Outside the update they fall back to `timeout` where the platform has one.
_le_timeout() { if command -v timeout >/dev/null 2>&1; then echo "timeout $1"; fi; }
LOCAL_ENGINE_RUNNER="${LOCAL_ENGINE_RUNNER:-$(_le_timeout 120)}"
LOCAL_ENGINE_UNLOAD_RUNNER="${LOCAL_ENGINE_UNLOAD_RUNNER:-$(_le_timeout 300)}"
LOCAL_ENGINE_BUILD_RUNNER="${LOCAL_ENGINE_BUILD_RUNNER:-$(_le_timeout 1800)}"
# How long to wait for a stopped engine's memory to come back, and how often to look.
LOCAL_ENGINE_SETTLE_S="${LOCAL_ENGINE_SETTLE_S:-90}"
LOCAL_ENGINE_POLL_S="${LOCAL_ENGINE_POLL_S:-3}"

LOCAL_ENGINE_FLASH_NEXT_IMAGE="jbrain2-flash-next:local"

# The service each engine runs as (jbrain.llm.engine.SERVICE). The compose PROFILE carries
# the same name, which is why one variable serves both.
local_engine_service() {
  case "$1" in
    flash-next) echo flash-next ;;
    *) echo local-llm ;;
  esac
}

_le_last_line() { printf '%s\n' "${1:-}" | tr -d '\r' | sed '/^[[:space:]]*$/d' | tail -n1 | tr -d ' '; }

# One engine name out of whatever the CLI printed: the last non-blank line, so compose's own
# chatter or a TIMEOUT line from a bounded runner cannot be mistaken for a value, and
# anything unrecognised (an old api image without the subcommand, a DB blip) is `standard`.
local_engine_parse() {
  case "$(_le_last_line "${1:-}")" in
    flash-next) echo flash-next ;;
    *) echo standard ;;
  esac
}

# The selected engine, read from the settings store.
local_engine_read() {
  # shellcheck disable=SC2086  # the runner is a deliberately word-split command prefix.
  local_engine_parse "$($LOCAL_ENGINE_RUNNER docker compose run --rm --no-deps -T api python -m jbrain.cli local-engine 2>/dev/null || true)"
}

# The ids LOCAL_MODELS (.env) says are installed, space-separated. That line is written only
# by automation (the model sync), so it is the record of what has weights on disk.
local_engine_installed_ids() {
  grep '^LOCAL_MODELS=' .env 2>/dev/null | tail -n1 | sed 's/^LOCAL_MODELS=//' \
    | tr -d '[]" ' | tr ',' ' ' || true
}

# Whether any id in $1 (a whitespace list) is served by the Flash-Next engine, by the catalog's
# own `engine` field — so a later Flash-Next variant needs no edit here. Prints `yes`, `no` or
# `unknown`: a catalog read that times out or fails is NOT a "no", because callers act
# destructively on "no" (deleting the image, the config file). An empty list is a clean `no`.
local_engine_flash_next_answer() {
  if [ -z "$(printf '%s' "${1:-}" | tr -d '[:space:]')" ]; then
    echo no
    return 0
  fi
  # shellcheck disable=SC2086  # the runner is a deliberately word-split command prefix.
  _le_out="$($LOCAL_ENGINE_RUNNER docker compose run --rm --no-deps -T -e IDS="$1" api python -c '
import os
from jbrain.llm import local_catalog
ids = os.environ["IDS"].split()
print("yes" if any(getattr(local_catalog.get(i), "engine", "standard") == "flash-next" for i in ids) else "no")
' 2>/dev/null || true)"
  case "$(_le_last_line "$_le_out")" in
    yes) echo yes ;;
    no) echo no ;;
    *) echo unknown ;;
  esac
}

# Succeeds only on a definite yes.
local_engine_any_flash_next() { [ "$(local_engine_flash_next_answer "${1:-}")" = yes ]; }

local_engine_flash_next_image_present() {
  docker image inspect "$LOCAL_ENGINE_FLASH_NEXT_IMAGE" >/dev/null 2>&1
}

# "1" when Flash-Next can run: a Flash-Next model is installed per LOCAL_MODELS AND its image
# exists (nothing here builds one). Else empty. A value rather than a status so it can be
# captured once and passed to local_engine_start.
local_engine_flash_next_installed() {
  if local_engine_any_flash_next "$(local_engine_installed_ids)" \
      && local_engine_flash_next_image_present; then
    echo 1
  fi
}

# "Up" = holds (or is about to re-take) its memory: running, paused, restarting or removing —
# the one predicate the supervisor (gateway.ENGINE_UP_STATES), the api (llm.engine.UP_STATES)
# and the perplexity job share. A crash-looping (restarting) engine re-allocates on every loop,
# so it is released and stopped like a running one, never skipped as down.
_le_running() {
  [ -n "$(docker compose --profile "$1" ps -q --status running --status paused \
    --status restarting --status removing "$1" 2>/dev/null)" ]
}

_le_mem_available_kb() {
  awk '/^MemAvailable:/ { print $2; exit }' /proc/meminfo 2>/dev/null || echo 0
}

# Take ONE engine down without dumping its memory on the kernel all at once: release its
# models through the gateway (the `local-llm` alias reaches whichever engine is up), stop
# it, then wait — bounded — until the container is no longer running and MemAvailable has
# stopped climbing. Starting the other engine while tens of GB are still being reclaimed is
# the same allocation collision the update's pre-build release exists to avoid.
local_engine_release() {
  _le_svc="$1"
  if ! _le_running "$_le_svc"; then
    docker compose --profile "$_le_svc" stop "$_le_svc" >/dev/null 2>&1 || true
    return 0
  fi
  local_engine_say "releasing $_le_svc's models before stopping it"
  # shellcheck disable=SC2086  # the runner is a deliberately word-split command prefix.
  $LOCAL_ENGINE_UNLOAD_RUNNER docker compose run --rm --no-deps -T api \
    python -m jbrain.cli local-llm-unload >/dev/null 2>&1 \
    || local_engine_say "unload skipped ($_le_svc unreachable?)"
  docker compose --profile "$_le_svc" stop "$_le_svc" >/dev/null 2>&1 || true
  _le_waited=0
  _le_prev="$(_le_mem_available_kb)"
  while [ "$_le_waited" -lt "$LOCAL_ENGINE_SETTLE_S" ]; do
    sleep "$LOCAL_ENGINE_POLL_S"
    _le_waited=$((_le_waited + LOCAL_ENGINE_POLL_S))
    [ "$LOCAL_ENGINE_POLL_S" -gt 0 ] || _le_waited=$((_le_waited + 1))
    _le_now="$(_le_mem_available_kb)"
    # Settled: the container is down and memory grew by under 512 MB since the last look.
    if ! _le_running "$_le_svc" && [ $((_le_now - _le_prev)) -lt 524288 ]; then
      local_engine_say "$_le_svc stopped; memory settled after ${_le_waited}s"
      return 0
    fi
    _le_prev="$_le_now"
  done
  local_engine_say "WARNING: $_le_svc not settled after ${_le_waited}s — continuing"
  return 0
}

# Record the engine that is actually up as the EFFECTIVE engine, for the api. Best-effort: an
# unreachable DB leaves the last value, and the next start writes it again.
local_engine_set_effective() {
  # shellcheck disable=SC2086  # the runner is a deliberately word-split command prefix.
  $LOCAL_ENGINE_RUNNER docker compose run --rm --no-deps -T api \
    python -m jbrain.cli set-local-engine-effective "$1" >/dev/null 2>&1 \
    || local_engine_say "WARNING: could not record $1 as the effective engine"
}

# Bring up exactly ONE engine.
#
#   $1  the selected engine (standard | flash-next)
#   $2  "1" when Flash-Next can run (local_engine_flash_next_installed), else empty
#
# The other engine is RELEASED FIRST (local_engine_release) and then left CREATED but not
# running (`up --no-start`): the supervisor's /start only starts a container that exists (404
# otherwise), and every update removes both, so without this a switch made after an update
# would have nothing to start (§4d "Provisioning"). Flash-Next is created only when it can run.
#
# Flash-Next selected but not runnable, or failing to start, falls back to the standard
# gateway and SAYS SO — the owner reads this in the PWA's update log, and a box with no
# engine at all is worse than one on the other engine. The DESIRED setting is left alone
# (rewriting the owner's choice is the switch's job, F3), so the next update retries it; the
# EFFECTIVE engine is recorded as standard, so the api serves standard meanwhile.
#
# Sets LOCAL_ENGINE_STARTED to the engine actually started, and records it as effective.
local_engine_start() {
  _le_engine="${1:-standard}"
  _le_fn="${2:-}"
  LOCAL_ENGINE_STARTED=''
  if [ "$_le_engine" = flash-next ] && [ "$_le_fn" != 1 ]; then
    local_engine_say "Flash-Next is the selected engine but is not installed or has no image — starting the standard engine instead (install it from Settings -> On-box models, then Ops -> Update)"
    _le_engine=standard
  fi
  if [ "$_le_engine" = flash-next ]; then
    local_engine_release local-llm
    docker compose --profile local-llm up --no-start --no-build local-llm >/dev/null 2>&1 \
      || local_engine_say "WARNING: could not create the standard gateway (stopped) — switching back will need an Update first"
    if docker compose --profile flash-next up -d --no-build flash-next; then
      LOCAL_ENGINE_STARTED=flash-next
      local_engine_set_effective flash-next
      local_engine_say "Flash-Next engine started; the standard gateway is created and stopped"
      return 0
    fi
    local_engine_say "WARNING: the Flash-Next engine did not start — falling back to the standard engine"
  fi
  if [ "${1:-standard}" = flash-next ]; then
    local_engine_say "FALLBACK: Flash-Next stays SELECTED (the next update retries it) but the standard engine serves — the api routes to standard until then"
  fi
  local_engine_release flash-next
  if [ "$_le_fn" = 1 ]; then
    docker compose --profile flash-next up --no-start --no-build flash-next >/dev/null 2>&1 \
      || local_engine_say "WARNING: could not create the Flash-Next container (stopped)"
  fi
  LOCAL_ENGINE_STARTED=standard
  _le_rc=0
  docker compose --profile local-llm up -d --no-build local-llm || _le_rc=$?
  local_engine_set_effective standard
  return "$_le_rc"
}

# Rebuild ONE engine's image (`local-llm` or `flash-next`) for deploy/refresh-inner.sh and
# deploy/rebuild-inner.sh, without ever putting a second engine up.
#
# A plain `up -d <service>` there would enable that service's profile and START it — beside the
# other engine if that one serves (§4d's freeze) — and its implicit build is a llama.cpp
# compile on a serving box. So an engine refresh is a QUIESCED build, the same discipline as
# the update's: release whichever engine is up (models unloaded, stopped, memory settled),
# build under the bounded runner with nothing serving, recreate the container STOPPED
# (`up --no-start --no-build`), then bring back exactly the engine that was up before — the
# refreshed one or the other — through local_engine_start (one engine, effective recorded,
# fallback to standard if Flash-Next will not start). Nothing up before: nothing started.
#
# This is what lets F2 iterate on the flash-next image whichever engine serves, at the cost of
# local inference being down for the build; the supervisor refuses any engine /start or
# /restart while the one-shot runs, so nothing restarts one underneath the compile.
#
# A failed build keeps the previous image (compose leaves it), still brings the engine back,
# and returns non-zero so the one-shot reads as failed.
local_engine_rebuild() {
  _le_target="$1"
  _le_was=''
  for _le_svc in local-llm flash-next; do
    if _le_running "$_le_svc"; then _le_was="$_le_was $_le_svc"; fi
  done
  case "$_le_was" in
    *local-llm*flash-next*)
      # Both up is the violation itself: bring back the one the owner selected.
      _le_back="$(local_engine_read)" ;;
    *flash-next*) _le_back=flash-next ;;
    *local-llm*) _le_back=standard ;;
    *) _le_back='' ;;
  esac
  for _le_svc in $_le_was; do
    local_engine_release "$_le_svc"
  done
  _le_brc=0
  local_engine_say "building $_le_target with no engine running (bounded)"
  # shellcheck disable=SC2086  # the runner is a deliberately word-split command prefix.
  $LOCAL_ENGINE_BUILD_RUNNER docker compose --profile "$_le_target" build "$_le_target" \
    || { _le_brc=1; local_engine_say "WARNING: $_le_target did not build — keeping its previous image"; }
  docker compose --profile "$_le_target" up --no-start --no-build "$_le_target" >/dev/null 2>&1 \
    || { _le_brc=1; local_engine_say "WARNING: could not recreate $_le_target (stopped)"; }
  if [ -n "$_le_back" ]; then
    local_engine_say "bringing back the engine that was running: $_le_back"
    local_engine_start "$_le_back" "$(local_engine_flash_next_installed)" || _le_brc=1
  else
    local_engine_say "no engine was running before; $_le_target is left created and stopped"
  fi
  return "$_le_brc"
}

# The update's engine decision, run once after the image build (update-inner.sh). Sets:
#   SELECTED_ENGINE   the owner's setting (an unreadable one is `standard`)
#   ENGINE            the engine this update works with: SELECTED_ENGINE, or `standard` when
#                     Flash-Next is selected but not ready — and the log says so
#   FLASH_NEXT_READY  "1" when a Flash-Next model is installed and its image exists
#
# Flash-Next's image is BUILT here, bounded, only when one of its models is installed
# (LOCAL_MODELS) or queued for install and not queued for removal — a box that never
# provisions it never compiles its llama.cpp, and a first install has its image ready when the
# model sync lands the weights rather than one update later. Its image is DELETED only on a
# definite "no" from the catalog: a read that failed (timeout, DB, an old api image) leaves it.
local_engine_select() {
  SELECTED_ENGINE="$(local_engine_read)"
  ENGINE="$SELECTED_ENGINE"
  FLASH_NEXT_READY=''
  # shellcheck disable=SC2086  # the runner is a deliberately word-split command prefix.
  _le_queued="$($LOCAL_ENGINE_RUNNER docker compose run --rm --no-deps -T api \
    python -m jbrain.cli local-provision-ids 2>/dev/null || true)"
  # shellcheck disable=SC2086  # the runner is a deliberately word-split command prefix.
  _le_removing="$($LOCAL_ENGINE_RUNNER docker compose run --rm --no-deps -T api \
    python -m jbrain.cli local-remove-ids 2>/dev/null || true)"
  _le_ids=''
  for _le_id in $(local_engine_installed_ids) $_le_queued; do
    # Catalog ids only: a TIMEOUT line from a bounded runner is words, not ids.
    printf '%s' "$_le_id" | grep -Eq '^[A-Za-z0-9._-]+$' || continue
    if printf '%s\n' "$_le_removing" | grep -qxF "$_le_id"; then continue; fi
    _le_ids="$_le_ids $_le_id"
  done
  case "$(local_engine_flash_next_answer "$_le_ids")" in
    yes)
      local_engine_say "a Flash-Next model is installed or queued — building the flash-next engine image"
      # shellcheck disable=SC2086  # the runner is a deliberately word-split command prefix.
      $LOCAL_ENGINE_BUILD_RUNNER docker compose --profile flash-next build flash-next \
        || local_engine_say "WARNING: the flash-next image did not build — keeping its previous image, if any"
      if [ -n "$(local_engine_flash_next_installed)" ]; then FLASH_NEXT_READY=1; fi
      ;;
    no)
      # Uninstalled (or never installed): the pre-build release already removed its
      # container; drop the image too, so backing out from the PWA leaves nothing behind.
      docker image rm "$LOCAL_ENGINE_FLASH_NEXT_IMAGE" >/dev/null 2>&1 || true
      ;;
    *)
      local_engine_say "could not read the catalog — leaving the flash-next image as it is"
      if [ -n "$(local_engine_flash_next_installed)" ]; then FLASH_NEXT_READY=1; fi
      ;;
  esac
  if [ "$ENGINE" = flash-next ] && [ -z "$FLASH_NEXT_READY" ]; then
    local_engine_say "Flash-Next is the selected engine but is not installed or has no image — falling back to the standard engine for this update"
    ENGINE=standard
  fi
  local_engine_say "local engine: $ENGINE (selected: $SELECTED_ENGINE)"
  return 0
}
