#!/usr/bin/env bash
# OPT-IN: provision on-box fish identification (the fishial models) and enable it.
#
#   sudo bash scripts/fish-id-setup.sh                 # recommended set
#   sudo bash scripts/fish-id-setup.sh fishial-v2      # explicit ids
#
# The sibling of scripts/comfyui-setup.sh, tuned for an AMD Strix Halo box (gfx1151).
# NEVER run by the default install or dev-setup.sh — it downloads model weights and
# starts a GPU service, so it is a deliberate, separate step. It:
#   1. resolves the chosen catalog models (jbrain.fish_id.catalog),
#   2. downloads their weight files into ./fish-id-models/<subdir> (the layout the
#      fish-id service expects: classifier / detector / segmenter / database),
#   3. flips JBRAIN_FISH_ID_* on in .env and starts the `fish-id` compose profile.
#
# The MIT-licensed fishial models (fishial/fish-identification) ship as GitHub release
# assets. The exact release TAG and asset names are pinned by the F0 on-box spike
# (docs/FISH_ID_PLAN.md) — set FISH_ID_RELEASE to that tag; the per-file paths come from
# the catalog manifest. Run from the install dir (/opt/jbrain2) where docker-compose.yml
# + .env live.
set -euo pipefail

INSTALL_DIR="${JBRAIN_INSTALL_DIR:-/opt/jbrain2}"
MODELS_DIR="$INSTALL_DIR/fish-id-models"
# The GitHub release tag the weights are attached to — pinned by the F0 spike.
FISH_ID_RELEASE="${FISH_ID_RELEASE:-v2}"
cd "$INSTALL_DIR"

say() { printf '\n[fish-id] %s\n' "$*"; }

# Serialize runs — two concurrent provisions race on the same download dir. Hold an
# exclusive lock for the life of the script; fail fast otherwise.
exec 9>"$INSTALL_DIR/.fish-id-setup.lock"
if ! flock -n 9; then
  echo "[fish-id] another fish-id-setup run is already in progress — aborting." >&2
  exit 1
fi

DOWNLOAD_CONTAINER="jbrain-fish-id-download"
cleanup() { docker rm -f "$DOWNLOAD_CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM

[ -f .env ] || { echo "No .env in $INSTALL_DIR — run deploy/install.sh first." >&2; exit 1; }

if ! command -v python3 >/dev/null 2>&1; then
  say "Installing python3"
  apt-get update -qq && apt-get install -y -qq python3
fi

# The fishial ROCm stack needs BOTH compute (/dev/kfd) and render (/dev/dri) nodes.
for dev in /dev/kfd /dev/dri; do
  [ -e "$dev" ] || say "WARNING: $dev not found — the ROCm fish-id service needs an AMD GPU. Continuing anyway."
done

# Read from the catalog via the api image. --no-deps so a pure-Python dump doesn't
# wait on the database. Fail LOUDLY if the image isn't built — otherwise an empty
# result would silently fall through to "all models" instead of the requested set.
catalog() {
  if ! docker compose run --rm --no-deps -T api python "$@"; then
    echo "[fish-id] catalog read failed — is the api image built? run 'jbrain update' first." >&2
    exit 1
  fi
}

IDS=("$@")
if [ ${#IDS[@]} -eq 0 ]; then
  say "No models named; using the recommended set"
  RECO="$(catalog -c "from jbrain.fish_id import catalog; print('\n'.join(catalog.recommended_ids()))")"
  mapfile -t IDS <<<"$RECO"
fi
mapfile -t IDS < <(printf '%s\n' "${IDS[@]}" | grep -v '^[[:space:]]*$')
[ ${#IDS[@]} -gt 0 ] || { echo "[fish-id] no models to provision — aborting." >&2; exit 1; }
say "Selected models: ${IDS[*]}"

# The catalog is the single source of truth — read its JSON manifest so the script
# never hard-codes filenames (the `source` repo + each file's repo_path).
MANIFEST="$(catalog -m jbrain.fish_id.catalog "${IDS[@]}")"
[ -n "$MANIFEST" ] || { echo "[fish-id] empty manifest — aborting before download." >&2; exit 1; }

mkdir -p "$MODELS_DIR"

# Download each model's files from its GitHub release in a throwaway container, placing
# each by its basename under the catalog's dest_subdir. Downloads are idempotent (a file
# already present is skipped), so re-runs cost nothing.
say "Downloading weights into $MODELS_DIR (release $FISH_ID_RELEASE)"
TTY_FLAG=""
[ -t 1 ] && TTY_FLAG="-t"
docker run --rm $TTY_FLAG --name "$DOWNLOAD_CONTAINER" \
  -e MANIFEST="$MANIFEST" -e FISH_ID_RELEASE="$FISH_ID_RELEASE" \
  -v "$MODELS_DIR:/models" python:3.11-slim bash -c '
  set -euo pipefail
  apt-get update -qq && apt-get install -y -qq curl >/dev/null
  python - <<'PY'
import json, os, subprocess
release = os.environ["FISH_ID_RELEASE"]
for m in json.loads(os.environ["MANIFEST"]):
    for f in m["files"]:
        dest_dir = os.path.join("/models", f["dest_subdir"])
        os.makedirs(dest_dir, exist_ok=True)
        name = os.path.basename(f["repo_path"])
        dest = os.path.join(dest_dir, name)
        if os.path.exists(dest):
            print("== have", dest, flush=True)
            continue
        # GitHub release asset: https://github.com/<source>/releases/download/<tag>/<name>
        url = "https://github.com/" + f["source"] + "/releases/download/" + release + "/" + name
        print("==>", url, flush=True)
        subprocess.check_call(["curl", "-fSL", url, "-o", dest])
PY
'

# Flip the feature on in .env (idempotent): enabled flag, service URL, the selected
# catalog ids as a JSON array, and the GPU GIDs (shared with the comfyui/local-llm
# services). We do NOT persist COMPOSE_PROFILES — the `jbrain` helper activates the
# fish-id profile from FISH_ID_ENABLED so the service isn't dragged into unrelated commands.
RENDER_GID="$(stat -c %g /dev/dri/renderD128 2>/dev/null || getent group render | cut -d: -f3 || true)"
VIDEO_GID="$(getent group video | cut -d: -f3 || true)"
if [ -z "$RENDER_GID" ] && [ -z "$VIDEO_GID" ]; then
  say "WARNING: could not resolve a render/video GID — the service may be denied /dev/dri."
fi
FISH_ID_MODELS_JSON="$(printf '%s\n' "${IDS[@]}" | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')"
say "Enabling fish identification in .env"
sed -i '/^FISH_ID_ENABLED=/d; /^FISH_ID_URL=/d; /^FISH_ID_MODELS=/d' .env
{
  echo "FISH_ID_ENABLED=true"
  echo "FISH_ID_URL=http://fish-id:8200"
  echo "FISH_ID_MODELS=$FISH_ID_MODELS_JSON"
  # VIDEO_GID/RENDER_GID are shared with comfyui; set them only if not already present.
  grep -q '^VIDEO_GID=' .env || { [ -n "$VIDEO_GID" ] && echo "VIDEO_GID=$VIDEO_GID"; }
  grep -q '^RENDER_GID=' .env || { [ -n "$RENDER_GID" ] && echo "RENDER_GID=$RENDER_GID"; }
} >> .env

say "Starting the fish-id service"
docker compose --profile fish-id up -d fish-id

say "Done. Fish identification is now available to jerv. Each identification loads"
say "the model, classifies, and frees it (load -> use -> unload)."
