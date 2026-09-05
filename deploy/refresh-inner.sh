#!/bin/sh
# Pull main and rebuild ONE compose service — the fast path between `rebuild` (which
# applies code already on the box and never pulls) and `update` (which pulls and then
# rebuilds the world, taking ten minutes and unloading every model on the way).
#
# It exists because the sdr sidecar is pure Python behind an apt-only image, so a
# one-line change to a measurement costs a full system update to try. That turns every
# question about the radio into a ten-minute round trip, and the owner has no terminal
# to shortcut it with (CLAUDE.md #10) — so the shortcut has to be an operable route.
#
# **It takes no ref, and that is the security property**, exactly as `update` does not:
# the reset target is the tracked upstream, so a caller can ask for what a merged PR
# already put on `main` and nothing else. A ref parameter here would turn a capability
# token into remote code execution on the box.
#
# **It is a PARTIAL deploy, deliberately.** The source mirror moves to main for every
# service, but only the named one is rebuilt — so the api can be running older code than
# `src` describes until a full `update` follows. That is the right trade for iterating on
# one sidecar and the wrong one for shipping, which is why the log says so and the
# runbook says so louder. It also does NOT refresh the host helper files (compose,
# backup.sh, jbrain): a compose or Dockerfile-path change still needs a full update.
#
# $1 is the compose service name; the supervisor validates it against the live service
# set and shell-quotes it before this runs, so it is a known-safe token.
set -eu

SERVICE="${1:?refresh: missing service name}"

echo "[refresh] $SERVICE: pulling latest main"
# Same dubious-ownership dance as the full update: this runs as root in an ephemeral
# container against a worktree owned by the host operator's UID, and root's container
# HOME carries no gitconfig, so a host-side safe.directory never reaches here.
git config --global --add safe.directory "$PWD/src"
# Mirror the remote exactly rather than `pull --ff-only`, for the same reason the update
# does: a deploy box should never diverge, and if it has, ff-only would abort and pin the
# stack to stale source. `@{u}` is the tracked upstream — there is no ref to pass in.
git -C src fetch origin
git -C src reset --hard "@{u}"
echo "[refresh] source now at $(git -C src rev-parse HEAD)"

echo "[refresh] $SERVICE: building image"
docker compose build "$SERVICE"

echo "[refresh] $SERVICE: recreating container"
docker compose up -d "$SERVICE"

echo "[refresh] $SERVICE: done — PARTIAL deploy, only this service was rebuilt"
