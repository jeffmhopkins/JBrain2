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

echo "[rebuild] $SERVICE: building image"
docker compose build "$SERVICE"

echo "[rebuild] $SERVICE: recreating container"
docker compose up -d "$SERVICE"

echo "[rebuild] $SERVICE: done"
