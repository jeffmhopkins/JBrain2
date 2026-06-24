#!/bin/sh
# Provision the operator's locally-hosted LLM models at the tail of an update —
# the PWA's no-shell path to installing a model (e.g. a big 3-bit MoE) onto the
# box. Invoked by deploy/update-inner.sh (the PWA update one-shot) and by
# `jbrain update` (host parity), AFTER the stack is back up.
#
# When local hosting is enabled it provisions the UNION of:
#   1. the PWA install queue   (settings store, owner-scoped, via jbrain.cli),
#   2. the current LOCAL_MODELS (.env),
#   3. the recommended set      (the catalog),
# then: downloads any missing weights, re-stamps llama-swap.yaml, rewrites only
# LOCAL_MODELS in .env (every other line — the GPU GIDs, the gateway URL — is
# preserved, which is WHY this does not re-run the host setup script in the
# bash-less, GPU-less updater), restarts the gateway + api, and clears the queue.
#
# Idempotent: an unchanged set re-runs as a cheap no-op (huggingface skips files
# already present). Best-effort by contract — callers tolerate a non-zero exit so
# a provisioning hiccup never aborts the update; the queue persists for a retry.
#
# POSIX sh (runs in the bash-less docker:cli one-shot) and no host python3 (the
# updater has none): every Python step runs inside the api container.
#
# Run from the install dir (cwd holds docker-compose.yml + .env + ./src).
set -eu

say() { printf '\n[local-llm] %s\n' "$*"; }

[ -f .env ] || { say "no .env — skipping model sync"; exit 0; }
grep -q '^LOCAL_LLM_ENABLED=true' .env || { say "hosting off — skipping model sync"; exit 0; }

# Catalog reads run in the api image (pure Python; --no-deps skips the database).
catalog() { docker compose run --rm --no-deps -T api python "$@"; }

# 1. The PWA install queue (owner-scoped DB read). This runs after `up -d`, so the
#    db is up. A clean empty result is the normal "nothing queued" case.
requested="$(docker compose run --rm -T api python -m jbrain.cli local-provision-ids || true)"

# 2. Current selection from .env (LOCAL_MODELS=["a","b"] -> newline list; ids have
#    no spaces/quotes, so strip the JSON punctuation) + the recommended set.
current="$(grep '^LOCAL_MODELS=' .env | sed 's/^LOCAL_MODELS=//' | tr -d '[]" ' | tr ',' '\n' || true)"
reco="$(catalog -c "from jbrain.llm import local_catalog; print('\n'.join(local_catalog.recommended_ids()))" || true)"

# 3. Union (blank lines dropped). Word-split into $ids on whitespace below.
ids="$(printf '%s\n%s\n%s\n' "$requested" "$current" "$reco" | grep -v '^[[:space:]]*$' | sort -u | tr '\n' ' ')"
[ -n "$ids" ] || { say "no models to sync"; exit 0; }
say "syncing models: $ids"

# 4. Manifest for the union; download any missing weights (idempotent). The
#    download streams into this script's stdout -> the update log, so the PWA can
#    follow it; the per-model % bar reads on-disk bytes via the settings API.
# shellcheck disable=SC2086  # $ids is a deliberately word-split id list.
manifest="$(catalog -m jbrain.llm.local_catalog $ids)"
[ -n "$manifest" ] || { say "empty manifest — aborting sync"; exit 1; }
# Absolute models dir: `docker run -v` resolves its source on the daemon, which has
# no notion of this script's cwd, so a relative `./local-models` is not the host
# path the api reads — pass the absolute path (cwd is the install dir).
MANIFEST="$manifest" DOWNLOAD_CONTAINER="jbrain-local-models-sync-dl" \
  sh src/deploy/download-local-weights.sh "$PWD/local-models"

# 5. Re-stamp llama-swap.yaml for the new set (the api re-renders it, resolving
#    each glob to a real downloaded filename). resident_group stays off unless the
#    operator persisted it — every model swappable is the safe default, and the
#    only one a 100+ GB model can use.
#    --user 0: the bind-mounted weights dir is root-owned (sudo setup + the root
#    download container), but the api image runs as non-root appuser, so the default
#    user can't create llama-swap.yaml there. Write as root, like the weights.
resident="$(grep '^LOCAL_LLM_RESIDENT_GROUP=' .env | cut -d= -f2- || true)"
docker compose run --rm --no-deps -T --user 0 \
  -e MANIFEST="$manifest" \
  -e LOCAL_LLM_RESIDENT_GROUP="$resident" \
  api python -m jbrain.llm.llama_swap_config /data/local-models

# 6. Rewrite only LOCAL_MODELS in .env to the union (build the JSON array in sh —
#    no host python). Every other line is left untouched.
json='['
sep=''
for id in $ids; do
  json="${json}${sep}\"${id}\""
  sep=','
done
json="${json}]"
sed -i '/^LOCAL_MODELS=/d' .env
echo "LOCAL_MODELS=$json" >> .env

# 7. Restart the gateway + api so the new config and LOCAL_MODELS take effect (api
#    reads LOCAL_MODELS at boot; the gateway reloads the swap config). The explicit
#    profile starts the gateway even though the update's plain `up -d` skips it.
docker compose --profile local-llm up -d

# 8. Clear the queue — everything requested is now provisioned and enabled, so it
#    must stop showing as queued. Best-effort: a missed clear only leaves stale
#    rows that the next sync would re-provision as a no-op.
docker compose run --rm -T api python -m jbrain.cli local-provision-clear || true
say "model sync complete"
