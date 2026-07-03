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

# 1b. The PWA UNINSTALL queue (the mirror). These ids are SUBTRACTED from the union
#    below so a removed model drops out of LOCAL_MODELS / the manifest / llama-swap
#    and stops being served — and, behind hard guards, has its weights pruned.
removing="$(docker compose run --rm -T api python -m jbrain.cli local-remove-ids || true)"

# 2. Current selection from .env (LOCAL_MODELS=["a","b"] -> newline list; ids have
#    no spaces/quotes, so strip the JSON punctuation) + the recommended set.
current="$(grep '^LOCAL_MODELS=' .env | sed 's/^LOCAL_MODELS=//' | tr -d '[]" ' | tr ',' '\n' || true)"
reco="$(catalog -c "from jbrain.llm import local_catalog; print('\n'.join(local_catalog.recommended_ids()))" || true)"

# 3. Union (blank lines dropped), THEN subtract the uninstall queue. Precedence is
#    deliberate: the subtraction is applied LAST so a removed id can never be
#    resurrected by the recommended set (uninstalling a recommended model like
#    gpt-oss-120b must stick until the operator re-installs it). `grep -vxF` against
#    a temp file of removing ids is the set difference (whole-line, fixed-string).
union="$(printf '%s\n%s\n%s\n' "$requested" "$current" "$reco" | grep -v '^[[:space:]]*$' | sort -u)"
remove_file="$(mktemp)"
trap 'rm -f "$remove_file"' EXIT
printf '%s\n' "$removing" | grep -v '^[[:space:]]*$' | sort -u > "$remove_file"
ids="$(printf '%s\n' "$union" | grep -vxF -f "$remove_file" | tr '\n' ' ')"
# Empty `$ids` is a VALID terminal state now that the uninstall queue is subtracted
# above: "uninstall every served model" leaves an empty roster that must still be
# APPLIED (write LOCAL_MODELS=[], restart, prune, clear) — bailing here would wedge
# the removal forever. Only a truly idle run (nothing queued AND nothing to remove)
# is a no-op. NB: `_manifest([])` returns the FULL catalog, so the download/swap
# steps below are gated on a non-empty `$ids` to avoid re-pulling everything.
if [ -z "$ids" ] && [ ! -s "$remove_file" ]; then say "no models to sync"; exit 0; fi
say "syncing models: ${ids:-<none>}"

if [ -n "$ids" ]; then
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
  #    each glob to a real downloaded filename). resident_group defaults OFF (opt-in):
  #    an absent/empty .env value reads as off, so the recommended set swaps one at a
  #    time — co-residency pins ~91 GB and destabilised the box; opt IN with
  #    LOCAL_LLM_RESIDENT_GROUP=1 on a box with memory to spare.
  #    --user 0: the bind-mounted weights dir is root-owned (sudo setup + the root
  #    download container), but the api image runs as non-root appuser, so the default
  #    user can't create llama-swap.yaml there. Write as root, like the weights.
  resident="$(grep '^LOCAL_LLM_RESIDENT_GROUP=' .env | cut -d= -f2- || true)"
  docker compose run --rm --no-deps -T --user 0 \
    -e MANIFEST="$manifest" \
    -e LOCAL_LLM_RESIDENT_GROUP="$resident" \
    api python -m jbrain.llm.llama_swap_config /data/local-models
else
  # Empty roster: every served model was uninstalled. Skip download/swap (nothing to
  # fetch; `_manifest([])` would pull the whole catalog), but still apply the removal
  # below — LOCAL_MODELS=[], restart, prune, clear.
  say "no models remain enabled — clearing local roster"
fi

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

# 7b. Prune the weights for uninstalled models (DESTRUCTIVE — see prune-local-weights.sh
#    for the four hard guards). The model is ALREADY out of LOCAL_MODELS / the gateway
#    by this point (step 6/7), so the safety requirement is met even if this fails;
#    deletion only reclaims disk. Pass the FINAL keep set ($ids) and the remove set so
#    the prune can never touch a model we still serve. Best-effort, like the rest.
# shellcheck disable=SC2086  # the removing ids are a deliberately word-split list.
KEEP="$ids" sh src/deploy/prune-local-weights.sh "$PWD/local-models" $(cat "$remove_file") || true

# 8. Clear the queues — everything requested is now provisioned and enabled, and every
#    uninstall has been applied, so both must stop showing as queued. Best-effort: a
#    missed clear only leaves stale rows that the next sync would re-apply as a no-op.
docker compose run --rm -T api python -m jbrain.cli local-provision-clear || true
docker compose run --rm -T api python -m jbrain.cli local-remove-clear || true
say "model sync complete"
