#!/bin/sh
# Rebuild ONE compose service, launched by the supervisor as a detached one-shot
# (docker:cli image) so it survives the target service — even the api/proxy it
# recreates — restarting beneath it. The project dir is mounted at its real host
# path, so compose's relative bind + build paths resolve correctly.
#
# A targeted subset of `jbrain update`: no git pull, no backup, no migrate — just
# `docker compose build <svc>` (a no-op for an image-only service) then `up -d <svc>`
# to recreate it. Used to apply a code/Dockerfile change already on the box (e.g. a
# new baked tts-stt voice) without a full system update.
#
# $1 is the compose service name; the supervisor validates it against the live
# service set and shell-quotes it before this runs, so it is a known-safe token.
set -eu

SERVICE="${1:?rebuild: missing service name}"

# An on-box LLM engine is never `up -d`'d here: naming a profiled service enables its profile,
# so that would START it — beside the other engine if that one serves (FLASH_NEXT_ENGINE_PLAN
# §4d, a freeze) — after a llama.cpp compile on a serving box. local_engine_rebuild builds it
# with no engine running, recreates it stopped, and brings back only the engine that was up.
case "$SERVICE" in
  local-llm|flash-next)
    . src/deploy/local-engine.sh
    echo "[rebuild] $SERVICE is an on-box engine: quiesced build, one engine back after"
    rc=0
    local_engine_rebuild "$SERVICE" || rc=$?
    echo "[rebuild] $SERVICE: done (exit $rc)"
    exit "$rc"
    ;;
esac

echo "[rebuild] $SERVICE: building image"
docker compose build "$SERVICE"

echo "[rebuild] $SERVICE: recreating container"
docker compose up -d "$SERVICE"

echo "[rebuild] $SERVICE: done"
