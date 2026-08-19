#!/bin/sh
# DESTRUCTIVE — deletes primed-prefix KV state files. Review the four guards before editing.
#
# The `llm_kv` volume holds llama-server's KV-slot state (`--slot-save-path /kv/`), written by
# the warm keeper so a cold start restores jerv's primed prefix instead of paying a ~100 s
# prefill. Each file is ~1 GB (gpt-oss) to ~2 GB (the 27B hybrid), and NOTHING else reclaims
# them: the name carries a fingerprint, so a superseded file simply stops being read and sits
# there forever. An llama.cpp rebuild changes `build_info` and orphans one per model; so does a
# persona edit, a tool change, or an `-c`/`-np` change.
#
# So this runs from the UPDATE (deploy/update-inner.sh) — the moment orphans are actually
# created, and the only recurring host-side job that can reach the volume at all. The api
# container does not mount `llm_kv`, which is why this is a docker one-shot rather than a
# `jbrain.cli` command.
#
# Keeps the NEWEST $KEEP_PER_MODEL files per model (default 2) and deletes the rest. Newest, not
# all: after an update that did NOT rebuild llama.cpp the fingerprint is unchanged, so the
# surviving file is still live and wiping it would cost exactly the cold prefill this whole
# feature exists to avoid. Two rather than one because the prime's tool set can legitimately
# alternate (ComfyUI down hides the image tools), which yields two live fingerprints.
#
# Contract:
#   $1               — the docker volume name (default "${COMPOSE_PROJECT_NAME:-jbrain}_llm_kv").
#   KEEP_PER_MODEL   — env, how many newest files to keep per model (default 2).
#   JBRAIN_UPDATER_IMAGE — env, the one-shot image (default docker:cli, as update-inner.sh).
#
# A file is deleted ONLY when ALL FOUR guards pass (fail-closed — any miss skips it):
#   1. shape: the name matches jerv-<model>-<16 lowercase hex>.bin exactly;
#   2. basename: the name contains no "/" and is not "." or ".."  — nothing can address out;
#   3. type: it is a regular file in /kv (not a directory, not a symlink);
#   4. rank: it is NOT among the newest $KEEP_PER_MODEL for its model.
#
# POSIX sh (runs in the bash-less one-shot). Best-effort: a failed delete only leaves disk used.
set -u

say() { printf '\n[prune-kv] %s\n' "$*"; }

VOLUME="${1:-${COMPOSE_PROJECT_NAME:-jbrain}_llm_kv}"
KEEP_PER_MODEL="${KEEP_PER_MODEL:-2}"
HELPER_IMAGE="${JBRAIN_UPDATER_IMAGE:-docker:cli}"

case "$KEEP_PER_MODEL" in
  ''|*[!0-9]*) say "bad KEEP_PER_MODEL ($KEEP_PER_MODEL) — skipping"; exit 0 ;;
esac
[ "$KEEP_PER_MODEL" -ge 1 ] || { say "KEEP_PER_MODEL must be >= 1 — skipping"; exit 0; }

# The volume must already exist. Naming a missing one would make `docker run` CREATE it empty,
# which is harmless but hides a wrong name behind a silent success — so check and say so.
if ! docker volume inspect "$VOLUME" >/dev/null 2>&1; then
  say "no $VOLUME volume — nothing to prune"
  exit 0
fi

say "pruning $VOLUME (keeping the newest $KEEP_PER_MODEL per model)"

# The sweep itself runs INSIDE a container with the volume mounted — the only way to reach it;
# `--network none` because it has no reason to talk to anything. `ls -t` is newest-first, and a
# flat directory of a few files needs nothing cleverer.
docker run --rm --network none -v "$VOLUME:/kv" \
  -e "KEEP_PER_MODEL=$KEEP_PER_MODEL" "$HELPER_IMAGE" sh -s <<'INNER' || say "sweep skipped"
set -u
cd /kv 2>/dev/null || exit 0
seen="$(mktemp)" || exit 0
trap 'rm -f "$seen"' EXIT

for name in $(ls -t 2>/dev/null); do
  # Guard 1 — shape: only our own <model>-<16 hex> naming. Anything else in this volume was
  # not written by the keeper and is none of our business.
  model="$(printf '%s' "$name" | sed -n 's/^jerv-\(.*\)-[0-9a-f]\{16\}\.bin$/\1/p')"
  [ -n "$model" ] || continue

  # Guard 2 — basename: llama.cpp only ever writes bare basenames here, so anything carrying a
  # separator did not come from it and must not be resolved.
  case "$name" in
    */*|.|..) continue ;;
  esac

  # Guard 3 — type: a regular file, never a directory or a symlink pointing out of the volume.
  [ -f "$name" ] && [ ! -L "$name" ] || continue

  # Guard 4 — rank: `ls -t` walks newest-first, so the first KEEP_PER_MODEL sightings of a
  # model are its live ones and everything after is superseded.
  kept="$(grep -Fxc -- "$model" "$seen" 2>/dev/null || true)"
  [ -n "$kept" ] || kept=0
  if [ "$kept" -lt "$KEEP_PER_MODEL" ]; then
    printf '%s\n' "$model" >> "$seen"
    continue
  fi

  printf '[prune-kv] deleting superseded state: %s\n' "$name"
  rm -f -- "$name"
done
INNER
