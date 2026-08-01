#!/bin/sh
# prune-orphans.sh — remove Compose containers whose service no longer exists.
#
# When a service is RENAMED (server-brain->wall, whisper->tts-stt) or REMOVED
# (claude-shim) from docker-compose.yml, Compose leaves the old container behind
# with its com.docker.compose.{project,service} labels intact. The supervisor's
# list_containers(all=True) filters only on the project label, so that stale
# container keeps surfacing on the Ops screen — and a plain `down`/`up` never
# clears it, since Compose only manages services still declared in the file.
#
# We deliberately avoid `docker compose down --remove-orphans`: the update paths
# that call this run without the profile-gated services active (tunnel, local-llm,
# comfyui, jcode, tools, mqtt), so `--remove-orphans` would reap those running
# containers as "orphans" too. Instead we compute the set of services the file
# defines under ANY profile and remove only project-labeled containers whose
# service is not among them — profile-gated services are defined, so they survive.
#
# Run from the deploy dir (where docker-compose.yml lives), the same cwd the
# update paths use for their other `docker compose` calls.
set -eu

# Fixed by `name: jbrain` in docker-compose.yml (the compose project label).
PROJECT="jbrain"

# Every service defined in the file, across all profiles. `config --profiles`
# lists all profiles; feeding them all back to `config --services` yields the
# full set — the default selection omits profile-gated services, and sweeping on
# that partial set would wrongly reap them. If `--profiles` is unsupported (older
# Compose) the command fails; we bail rather than risk an incomplete set.
if ! all_profiles="$(docker compose config --profiles 2>/dev/null)"; then
  echo "[prune-orphans] compose config --profiles unavailable; skipping orphan sweep"
  exit 0
fi
profile_flags=""
for p in $all_profiles; do
  profile_flags="$profile_flags --profile $p"
done
# shellcheck disable=SC2086 -- word-splitting the flags is intentional
defined="$(docker compose $profile_flags config --services 2>/dev/null || true)"

# Safety net: an empty set means enumeration failed (compose error, docker
# hiccup). Sweeping then would delete the whole stack — so sweep nothing.
if [ -z "$defined" ]; then
  echo "[prune-orphans] could not read compose services; skipping orphan sweep"
  exit 0
fi

removed=""
for id in $(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" 2>/dev/null || true); do
  svc="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.service" }}' "$id" 2>/dev/null || true)"
  # No service label (empty, or Go's "<no value>" for a nil map) — not a Compose
  # service container we can reason about; leave it alone.
  case "$svc" in "" | "<no value>") continue ;; esac
  # Keep any container whose service is still defined (the live stack plus every
  # profile-gated service, active or not).
  if printf '%s\n' "$defined" | grep -qx "$svc"; then
    continue
  fi
  if docker rm -f "$id" >/dev/null 2>&1; then
    removed="$removed $svc"
  fi
done

if [ -n "$removed" ]; then
  echo "[prune-orphans] removed orphaned containers:$removed"
else
  echo "[prune-orphans] no orphaned containers to remove"
fi
