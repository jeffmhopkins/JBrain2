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
# MINUS the uninstall set: the PWA uninstall queue PLUS any DECOMMISSIONED catalog id
# (local_catalog.RETIRED_IDS) still present on the box — so retiring a model from the
# catalog uninstalls + prunes it on the next update, no shell needed (CLAUDE.md #10).
# Then: downloads any missing weights, re-stamps llama-swap.yaml, rewrites only
# LOCAL_MODELS in .env (every other line — the GPU GIDs, the gateway URL — is
# preserved, which is WHY this does not re-run the host setup script in the
# bash-less, GPU-less updater), restarts the gateway + api, points agent.turn at
# the just-installed model so it becomes the box's active chat model (step 7d), and
# clears the queue.
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

# 2b. DECOMMISSIONED catalog ids (local_catalog.RETIRED_IDS) — models dropped from the
#    catalog that must be actively uninstalled from any box still holding them, not just
#    hidden from the picker. Folded into the uninstall set below so their weights are
#    pruned on the next update (the operator can't `rm` them — CLAUDE.md #10). Filter to
#    the ones ACTUALLY PRESENT (in the .env roster or on disk) so a box that never had one
#    contributes nothing here and still hits the routine fast-path; once pruned, the next
#    update sees none present and the tombstone goes quiet.
retired="$(catalog -c "from jbrain.llm import local_catalog; print('\n'.join(local_catalog.retired_ids()))" || true)"
retire_present=''
for rid in $retired; do
  [ -n "$rid" ] || continue
  if printf '%s\n' "$current" | grep -qxF "$rid" || ls "$PWD"/local-models/"$rid"/*.gguf >/dev/null 2>&1; then
    retire_present="${retire_present} ${rid}"
  fi
done
[ -n "$retire_present" ] && say "retiring decommissioned models:${retire_present}"

# 3. Union (blank lines dropped), THEN subtract the uninstall set (the PWA queue PLUS
#    the present decommissioned ids). Precedence is deliberate: the subtraction is
#    applied LAST so a removed id can never be resurrected by the recommended set
#    (uninstalling a recommended model like gpt-oss-120b must stick until the operator
#    re-installs it; a retired id can never come back at all). `grep -vxF` against a temp
#    file of the remove ids is the set difference (whole-line, fixed-string).
union="$(printf '%s\n%s\n%s\n' "$requested" "$current" "$reco" | grep -v '^[[:space:]]*$' | sort -u)"
remove_file="$(mktemp)"
trap 'rm -f "$remove_file"' EXIT
printf '%s\n%s\n' "$removing" "$retire_present" | grep -v '^[[:space:]]*$' | sort -u > "$remove_file"
# Set difference union - removing. Only run `grep -vxF -f` when the remove file is
# NON-EMPTY: BusyBox grep (this runs in the docker:cli one-shot, Alpine/busybox — not
# GNU) treats an EMPTY -f pattern file as a pattern that matches EVERY line, so
# `grep -v` against it would drop the whole union → an empty `$ids` → a spurious "no
# models to sync" on every install where nothing is queued for removal (the common
# case). GNU grep returns all lines here, which is why this passed local testing but
# silently no-op'd on the box. With no removals, ids IS the union.
if [ -s "$remove_file" ]; then
  ids="$(printf '%s\n' "$union" | grep -vxF -f "$remove_file" | tr '\n' ' ')"
else
  ids="$(printf '%s\n' "$union" | grep -v '^[[:space:]]*$' | tr '\n' ' ')"
fi
# Empty `$ids` is a VALID terminal state now that the uninstall queue is subtracted
# above: "uninstall every served model" leaves an empty roster that must still be
# APPLIED (write LOCAL_MODELS=[], restart, prune, clear) — bailing here would wedge
# the removal forever. Only a truly idle run (nothing queued AND nothing to remove)
# is a no-op. NB: `_manifest([])` returns the FULL catalog, so the download/swap
# steps below are gated on a non-empty `$ids` to avoid re-pulling everything.
if [ -z "$ids" ] && [ ! -s "$remove_file" ]; then say "no models to sync"; exit 0; fi

# Fast path for a ROUTINE update: nothing is queued (install OR uninstall) and the desired roster
# already equals LOCAL_MODELS, so the download below would only RE-VERIFY weights already on disk —
# an ~2-minute `hf download` re-hash of every model for ZERO change, the dominant cost of a no-op
# update. Skip it, but only once a cheap on-disk check confirms each model's weights are present
# (a `*.gguf` glob, NOT an hf hash) so a missing/corrupt weight still falls through to a full,
# self-healing sync. The gateway is restarted by update-inner.sh after us regardless, exactly as
# the "no models to sync" exit above already relies on — so skipping the sync's own restart is safe.
requested_ids="$(printf '%s\n' "$requested" | grep -v '^[[:space:]]*$' | sort -u || true)"
current_ids="$(printf '%s\n' "$current" | grep -v '^[[:space:]]*$' | sort -u || true)"
if [ -z "$requested_ids" ] && [ ! -s "$remove_file" ] && [ "$union" = "$current_ids" ]; then
  missing=''
  for id in $ids; do
    # A `*.gguf` under the model dir means its weights landed; `ls` (no hash) keeps this cheap.
    ls "$PWD"/local-models/"$id"/*.gguf >/dev/null 2>&1 || missing="$missing $id"
  done
  if [ -z "$missing" ]; then
    say "roster unchanged and weights present — skipping re-download ($ids)"
    exit 0
  fi
  say "roster unchanged but weights missing for:$missing — running full sync"
fi

say "syncing models: ${ids:-<none>}"

if [ -n "$ids" ]; then
  # 4. Manifest for the FULL roster — this drives the llama-swap re-stamp below, which
  #    must describe every served model (present or freshly pulled), not just the ones
  #    downloaded this round.
  # shellcheck disable=SC2086  # $ids is a deliberately word-split id list.
  manifest="$(catalog -m jbrain.llm.local_catalog $ids)"
  [ -n "$manifest" ] || { say "empty manifest — aborting sync"; exit 1; }

  # 4b. Download ONLY the models whose weights are missing on disk. `hf download` re-hashes
  #     every file it is handed even when the bytes are already present (~2 min/model), so
  #     pulling the whole roster re-verifies models that never changed — the dominant cost
  #     when provisioning onto a box that already holds most of the set (adding one model to
  #     a ten-model roster re-verified all ten). Filter to the missing ones with the same
  #     cheap `ls *.gguf` presence check the routine-update fast-path above uses (NOT an hf
  #     hash): a genuinely missing or interrupted weight still falls through to a full,
  #     self-healing pull. The trade — a present-but-corrupt weight is not re-hashed — matches
  #     that fast-path's, and is backstopped: the re-stamp below runs resolve_weight over the
  #     WHOLE roster, so a present-but-INCOMPLETE weight (missing shards) still fails loudly
  #     there rather than being served, and an already-downloaded model stays wired in.
  download_ids=''
  present_ids=''
  for id in $ids; do
    # A `*.gguf` under the model dir means its weights landed; `ls` (no hash) keeps this cheap.
    if ls "$PWD"/local-models/"$id"/*.gguf >/dev/null 2>&1; then
      present_ids="${present_ids} ${id}"
    else
      download_ids="${download_ids} ${id}"
    fi
  done
  download_ids="${download_ids# }"
  present_ids="${present_ids# }"
  [ -n "$present_ids" ] && say "weights present — skipping re-verify for: ${present_ids}"

  if [ -n "$download_ids" ]; then
    # shellcheck disable=SC2086  # $download_ids is a deliberately word-split id list.
    dl_manifest="$(catalog -m jbrain.llm.local_catalog $download_ids)"
    [ -n "$dl_manifest" ] || { say "empty manifest — aborting sync"; exit 1; }
    # Log the provisioning plan (id -> repo (quant) include=glob) for the models actually
    # pulled — the first thing to read when a download fails. Best-effort (never fatal).
    # -e injects MAN into the api container (a bare `MAN=…` would set it only in THIS shell).
    docker compose run --rm --no-deps -T -e MAN="$dl_manifest" api python -c 'import json,os
for m in json.loads(os.environ["MAN"]):
    print("[local-llm]   %s <- %s (%s) include=%s" % (m["id"], m["hf_repo"], m["quant"], m["gguf_include"]))' \
      || true
    # Absolute models dir: `docker run -v` resolves its source on the daemon, which has no
    # notion of this script's cwd, so a relative `./local-models` is not the host path the api
    # reads — pass the absolute path (cwd is the install dir). Capture the download's exit code
    # so a failure is a LOUD, greppable line in the log (both the Ops screen and
    # /api/debug/provision/status read this tail) instead of a bare non-zero exit. On failure we
    # exit here: steps 5-8 (re-stamp, enable, restart, clear-queue) are deliberately skipped so a
    # half-downloaded model is never served, and the queue persists for a retry.
    set +e
    MANIFEST="$dl_manifest" DOWNLOAD_CONTAINER="jbrain-local-models-sync-dl" \
      sh src/deploy/download-local-weights.sh "$PWD/local-models"
    dl_rc=$?
    set -e
    if [ "$dl_rc" -ne 0 ]; then
      say "DOWNLOAD FAILED for [${download_ids}] (rc=${dl_rc}) — see the hf output above for the reason (404 / auth / disk / network). Models stay queued for a retry."
      exit "$dl_rc"
    fi
  else
    say "all roster weights already present — no downloads needed"
  fi

  # 5. Re-stamp llama-swap.yaml for the new set (the api re-renders it, resolving
  #    each glob to a real downloaded filename). Every model is a non-swapping group
  #    member — the app (jbrain.llm.residency) is the sole evictor, freeing the fewest
  #    models to hold the free-RAM floor before each load, so nothing pins ~91 GB.
  #    --user 0: the bind-mounted weights dir is root-owned (sudo setup + the root
  #    download container), but the api image runs as non-root appuser, so the default
  #    user can't create llama-swap.yaml there. Write as root, like the weights.
  docker compose run --rm --no-deps -T --user 0 \
    -e MANIFEST="$manifest" \
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

# 7c. Sweep stale huggingface .cache partials from every remaining model dir. An
#    interrupted/killed download leaves tens of GB of *.incomplete staging behind (hf
#    streams shards into <model>/.cache/huggingface/download/ and only promotes a shard
#    to the top level when it completes), which nothing else reclaims. The finished
#    *.gguf live ABOVE .cache, so dropping .cache never touches a usable weight — hf
#    recreates it on the next download. The `*/` glob matches only direct children, and
#    the realpath check rejects a symlinked-out cache. Best-effort, like the rest.
for cache in "$PWD"/local-models/*/.cache; do
  [ -d "$cache" ] || continue
  cache_abs="$(realpath "$cache" 2>/dev/null || true)"
  case "$cache_abs" in
    "$PWD"/local-models/*/.cache) : ;;
    *)                            continue ;;
  esac
  [ "$(dirname "$(dirname "$cache_abs")")" = "$PWD/local-models" ] || continue
  say "clearing partials: $(basename "$(dirname "$cache_abs")")/.cache"
  rm -rf -- "$cache_abs" || true
done

# 7d. Make the just-installed model the box's ACTIVE chat model (owner decision): re-point
#    agent.turn to the model the operator queued this update, so the WarmKeeper keeps THAT
#    model hot instead of the standing default (which otherwise leaves whatever the post-update
#    smoke-test probe loaded, e.g. gpt-oss-120b, resident). Only when a model was actually
#    INSTALLED this run — a routine/no-op update or a pure uninstall leaves routing untouched.
#    The install queue keeps insertion order, so `tail -n1` is the most-recently-added model;
#    it is confirmed to be in the FINAL served roster ($ids) so a same-update install+uninstall
#    can't activate a model that isn't served. Runs BEFORE the queue is cleared below (it reads
#    the queue captured at step 1). Best-effort: a failed activation only leaves routing as-is.
activate="$(printf '%s\n' "$requested" | grep -v '^[[:space:]]*$' | tail -n1 || true)"
case " $ids " in *" $activate "*) : ;; *) activate='' ;; esac
if [ -n "$activate" ]; then
  say "activating just-installed model as the active chat model (agent.turn): $activate"
  docker compose run --rm -T api python -m jbrain.cli local-activate "$activate" || true
fi

# 8. Clear the queues — everything requested is now provisioned and enabled, and every
#    uninstall has been applied, so both must stop showing as queued. Best-effort: a
#    missed clear only leaves stale rows that the next sync would re-apply as a no-op.
docker compose run --rm -T api python -m jbrain.cli local-provision-clear || true
docker compose run --rm -T api python -m jbrain.cli local-remove-clear || true
say "model sync complete"
