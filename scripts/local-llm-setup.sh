#!/usr/bin/env bash
# OPT-IN: provision self-hosted local models and enable the feature.
#
#   sudo bash scripts/local-llm-setup.sh                 # recommended set
#   sudo bash scripts/local-llm-setup.sh qwen3-vl-30b    # explicit ids
#
# Tuned for an AMD Strix Halo box (Ryzen AI Max+ 395). This is NEVER run by the
# default install or by dev-setup.sh — it downloads tens of GB of weights and
# starts a GPU service, so it is a deliberate, separate step. It:
#   1. resolves the chosen catalog models (jbrain.llm.local_catalog),
#   2. downloads their GGUF weights into ./local-models,
#   3. writes a llama-swap config fronting them on one OpenAI endpoint,
#   4. flips JBRAIN_LOCAL_LLM_ENABLED + LOCAL_MODELS on in .env and starts the
#      `local-llm` compose profile.
#
# Run from the install dir (/opt/jbrain2) where docker-compose.yml + .env live.
set -euo pipefail

INSTALL_DIR="${JBRAIN_INSTALL_DIR:-/opt/jbrain2}"
MODELS_DIR="$INSTALL_DIR/local-models"
cd "$INSTALL_DIR"

say() { printf '\n[local-llm] %s\n' "$*"; }

# Serialize runs. Two concurrent provisions race on the same download dir + HF
# lock and stack throwaway containers (a hang we hit in the wild: two stuck
# downloaders fighting over one .lock). Hold an exclusive lock for the life of
# the script; fail fast if another run already owns it.
exec 9>"$INSTALL_DIR/.local-llm-setup.lock"
if ! flock -n 9; then
  echo "[local-llm] another enable-local-models run is already in progress — aborting." >&2
  exit 1
fi

# Give the download container a stable name and force-remove it on ANY exit, so a
# Ctrl+C can't strand it still holding the HF download lock for the next run.
DOWNLOAD_CONTAINER="jbrain-local-llm-download"
cleanup() { docker rm -f "$DOWNLOAD_CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM

[ -f .env ] || { echo "No .env in $INSTALL_DIR — run deploy/install.sh first." >&2; exit 1; }

# python3 drives the JSON/config generation below; Ubuntu Server usually ships
# it, but make the dependency explicit since this script may run before any.
if ! command -v python3 >/dev/null 2>&1; then
  say "Installing python3"
  apt-get update -qq && apt-get install -y -qq python3
fi

# Best-effort GPU sanity check: Vulkan needs /dev/dri. Warn, don't block — the
# operator may know better (remote build host, different AMD part).
if [ ! -e /dev/dri ]; then
  say "WARNING: /dev/dri not found — the Vulkan gateway needs an AMD GPU. Continuing anyway."
fi

# gfx1151 needs kernel >= 6.18.4 (older has a known stability bug). Warn only:
# if the lower of {6.18.4, this kernel} isn't 6.18.4, this kernel is older.
KREL="$(uname -r)"
if [ "$(printf '6.18.4\n%s\n' "${KREL%%-*}" | sort -V | head -1)" != "6.18.4" ]; then
  say "WARNING: kernel $KREL is older than 6.18.4 — gfx1151 has a stability bug below that."
fi

# Weights are tens of GB; warn (don't block) if the install disk looks tight.
AVAIL_GB="$(df -BG --output=avail "$INSTALL_DIR" | tail -1 | tr -dc '0-9')"
if [ -n "$AVAIL_GB" ] && [ "$AVAIL_GB" -lt 120 ]; then
  say "WARNING: only ${AVAIL_GB} GB free on $INSTALL_DIR — the recommended set needs ~95 GB."
fi

# Helper: read from the catalog via the api image. --no-deps so a pure-Python
# dump doesn't wait on the database. Fail LOUDLY if the image isn't built —
# otherwise an empty result would silently fall through to "all models" (~219GB)
# instead of the requested set.
catalog() {
  if ! docker compose run --rm --no-deps -T api python "$@"; then
    echo "[local-llm] catalog read failed — is the api image built? run 'jbrain update' first." >&2
    exit 1
  fi
}

IDS=("$@")
if [ ${#IDS[@]} -eq 0 ]; then
  say "No models named; using the recommended set"
  RECO="$(catalog -c "from jbrain.llm import local_catalog; print('\n'.join(local_catalog.recommended_ids()))")"
  mapfile -t IDS <<<"$RECO"
fi
# Drop blank entries, then assert we actually have ids — never proceed empty.
mapfile -t IDS < <(printf '%s\n' "${IDS[@]}" | grep -v '^[[:space:]]*$')
[ ${#IDS[@]} -gt 0 ] || { echo "[local-llm] no models to provision — aborting." >&2; exit 1; }
say "Selected models: ${IDS[*]}"

# The catalog is the single source of truth — read its JSON manifest so the
# script never hard-codes repos or filenames.
MANIFEST="$(catalog -m jbrain.llm.local_catalog "${IDS[@]}")"
[ -n "$MANIFEST" ] || { echo "[local-llm] empty manifest — aborting before download." >&2; exit 1; }

mkdir -p "$MODELS_DIR"

# Download weights with the official huggingface_hub CLI in a throwaway container;
# only the --include globs from the manifest are pulled. Shared with the update
# one-shot's sync (deploy/local-models-sync.sh) so the download logic is defined
# once; we pass our DOWNLOAD_CONTAINER name so the trap above still cleans it up.
say "Downloading weights into $MODELS_DIR (this can take a while)"
MANIFEST="$MANIFEST" DOWNLOAD_CONTAINER="$DOWNLOAD_CONTAINER" \
  bash "$INSTALL_DIR/src/deploy/download-local-weights.sh" "$MODELS_DIR"

# Generate the llama-swap config with the SHARED generator
# (jbrain.llm.llama_swap_config) so the install-time config and the API's runtime
# re-stamp (after a context-window edit) can never drift. It runs in the api
# container, which mounts ./local-models writable at /data/local-models and can see
# the downloaded weights to resolve each glob to a real filename. Each model's
# catalog `context_window` becomes its llama-server `-c` (the value the router also
# reports to the PWA's context meter). The gateway paths inside the cmd are the
# gateway-container view (/models/...), which is the same host dir.
say "Writing $MODELS_DIR/llama-swap.yaml"
# --user 0: the weights dir is root-owned (this script + the root download
# container), but the api image runs as non-root appuser and can't create the
# config there. Write as root, matching the weights.
docker compose run --rm --no-deps -T --user 0 \
  -e MANIFEST="$MANIFEST" \
  -e LOCAL_LLM_RESIDENT_GROUP="${LOCAL_LLM_RESIDENT_GROUP:-}" \
  api python -m jbrain.llm.llama_swap_config /data/local-models

# The gateway container must join the HOST's video/render group GIDs to open
# /dev/dri/renderD128. Prefer the device's actual owning GID (authoritative);
# fall back to the named groups. Warn if we can't resolve any — without a numeric
# host GID the container likely can't open /dev/dri.
RENDER_GID="$(stat -c %g /dev/dri/renderD128 2>/dev/null || getent group render | cut -d: -f3 || true)"
VIDEO_GID="$(getent group video | cut -d: -f3 || true)"
if [ -z "$RENDER_GID" ] && [ -z "$VIDEO_GID" ]; then
  say "WARNING: could not resolve a render/video GID — the gateway may be denied /dev/dri."
fi

# Flip the feature on in .env (idempotent): enabled flag, gateway URL, the
# selected catalog ids as a JSON array, and the GPU GIDs. We do NOT persist
# COMPOSE_PROFILES — `jbrain` activates the local-llm profile from
# LOCAL_LLM_ENABLED, so the gateway isn't dragged into unrelated commands.
LOCAL_MODELS_JSON="$(printf '%s\n' "${IDS[@]}" | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')"
say "Enabling local hosting in .env"
sed -i '/^LOCAL_LLM_ENABLED=/d; /^LOCAL_LLM_URL=/d; /^LOCAL_MODELS=/d; /^COMPOSE_PROFILES=/d; /^VIDEO_GID=/d; /^RENDER_GID=/d' .env
{
  echo "LOCAL_LLM_ENABLED=true"
  echo "LOCAL_LLM_URL=http://local-llm:8080/v1"
  echo "LOCAL_MODELS=$LOCAL_MODELS_JSON"
  [ -n "$VIDEO_GID" ] && echo "VIDEO_GID=$VIDEO_GID"
  [ -n "$RENDER_GID" ] && echo "RENDER_GID=$RENDER_GID"
} >> .env

say "Building the gateway image and starting the stack"
docker compose --profile local-llm build local-llm
docker compose --profile local-llm up -d

say "Done. Local models are now selectable in Settings → LLM. They stay OFF as"
say "defaults — route specific tasks/tiers to them from that screen."
if [ ! -e /dev/kfd ] && [ -e /dev/dri ]; then
  say "Tip: for host tuning (unified-memory sizing, perf profile) see"
  say "  'jbrain strix-halo-host-setup' (one-time, needs a reboot)."
fi
