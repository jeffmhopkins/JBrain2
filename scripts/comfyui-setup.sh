#!/usr/bin/env bash
# OPT-IN: provision on-box image generation (ComfyUI + Qwen-Image) and enable it.
#
#   sudo bash scripts/comfyui-setup.sh                  # recommended set
#   sudo bash scripts/comfyui-setup.sh qwen-image       # explicit ids
#
# The sibling of scripts/local-llm-setup.sh, tuned for an AMD Strix Halo box
# (gfx1151). NEVER run by the default install or dev-setup.sh — it downloads tens
# of GB of weights and starts a GPU service, so it is a deliberate, separate step.
# It:
#   1. resolves the chosen catalog models (jbrain.image_gen.catalog),
#   2. downloads their weight files into ./comfyui-models/<subdir> (the layout
#      ComfyUI expects: diffusion_models / text_encoders / vae / loras / checkpoints),
#   3. prunes weight files no longer named by the catalog (so a model swap — e.g.
#      fp8 -> bf16 — reclaims the superseded file),
#   4. flips JBRAIN_COMFYUI_* on in .env and starts the `comfyui` compose profile.
#
# Run from the install dir (/opt/jbrain2) where docker-compose.yml + .env live.
set -euo pipefail

INSTALL_DIR="${JBRAIN_INSTALL_DIR:-/opt/jbrain2}"
MODELS_DIR="$INSTALL_DIR/comfyui-models"
cd "$INSTALL_DIR"

say() { printf '\n[comfyui] %s\n' "$*"; }

# Serialize runs — two concurrent provisions race on the same download dir + HF
# lock. Hold an exclusive lock for the life of the script; fail fast otherwise.
exec 9>"$INSTALL_DIR/.comfyui-setup.lock"
if ! flock -n 9; then
  echo "[comfyui] another comfyui-setup run is already in progress — aborting." >&2
  exit 1
fi

# Give the download container a stable name and force-remove it on ANY exit, so a
# Ctrl+C can't strand it still holding the HF download lock for the next run.
DOWNLOAD_CONTAINER="jbrain-comfyui-download"
cleanup() { docker rm -f "$DOWNLOAD_CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM

[ -f .env ] || { echo "No .env in $INSTALL_DIR — run deploy/install.sh first." >&2; exit 1; }

if ! command -v python3 >/dev/null 2>&1; then
  say "Installing python3"
  apt-get update -qq && apt-get install -y -qq python3
fi

# ComfyUI's ROCm stack needs BOTH compute (/dev/kfd) and render (/dev/dri) nodes —
# unlike the Vulkan local-llm path, which needs only /dev/dri. Warn, don't block.
for dev in /dev/kfd /dev/dri; do
  [ -e "$dev" ] || say "WARNING: $dev not found — ROCm ComfyUI needs an AMD GPU. Continuing anyway."
done

# gfx1151 needs kernel >= 6.18.4 (older has a known stability bug). Warn only.
KREL="$(uname -r)"
if [ "$(printf '6.18.4\n%s\n' "${KREL%%-*}" | sort -V | head -1)" != "6.18.4" ]; then
  say "WARNING: kernel $KREL is older than 6.18.4 — gfx1151 has a stability bug below that."
fi

# Read from the catalog via the api image. --no-deps so a pure-Python dump doesn't
# wait on the database. Fail LOUDLY if the image isn't built — otherwise an empty
# result would silently fall through to "all models" instead of the requested set.
catalog() {
  if ! docker compose run --rm --no-deps -T api python "$@"; then
    echo "[comfyui] catalog read failed — is the api image built? run 'jbrain update' first." >&2
    exit 1
  fi
}

IDS=("$@")
if [ ${#IDS[@]} -eq 0 ]; then
  say "No models named; using the recommended set"
  RECO="$(catalog -c "from jbrain.image_gen import catalog; print('\n'.join(catalog.recommended_ids()))")"
  mapfile -t IDS <<<"$RECO"
fi
mapfile -t IDS < <(printf '%s\n' "${IDS[@]}" | grep -v '^[[:space:]]*$')
[ ${#IDS[@]} -gt 0 ] || { echo "[comfyui] no models to provision — aborting." >&2; exit 1; }
say "Selected models: ${IDS[*]}"

# The catalog is the single source of truth — read its JSON manifest so the script
# never hard-codes repos or filenames.
MANIFEST="$(catalog -m jbrain.image_gen.catalog "${IDS[@]}")"
[ -n "$MANIFEST" ] || { echo "[comfyui] empty manifest — aborting before download." >&2; exit 1; }

# Warn (don't block) if the install disk looks tight for THIS run's selection — the
# requested models' total download from the manifest, not a fixed guess, so installing a
# small model (DreamShaper ~7 GB) doesn't print a scary ~58 GB warning.
NEED_GB="$(MANIFEST="$MANIFEST" python3 -c 'import json,os,math; print(math.ceil(sum(m["size_gb"] for m in json.loads(os.environ["MANIFEST"]))))')"
AVAIL_GB="$(df -BG --output=avail "$INSTALL_DIR" | tail -1 | tr -dc '0-9')"
if [ -n "$AVAIL_GB" ] && [ -n "$NEED_GB" ] && [ "$AVAIL_GB" -lt "$NEED_GB" ]; then
  say "WARNING: only ${AVAIL_GB} GB free on $INSTALL_DIR — the selected models need ~${NEED_GB} GB."
fi

mkdir -p "$MODELS_DIR"

# Download each model's files with the official huggingface_hub CLI in a throwaway
# container, placing each file by its basename under the ComfyUI subdir the catalog
# names. Downloads are idempotent (hf skips files already present), so re-runs and
# models that share the encoder/VAE cost nothing extra.
say "Downloading weights into $MODELS_DIR (this can take a while)"
TTY_FLAG=""
[ -t 1 ] && TTY_FLAG="-t"
docker run --rm $TTY_FLAG --name "$DOWNLOAD_CONTAINER" \
  -e MANIFEST="$MANIFEST" -v "$MODELS_DIR:/models" python:3.11-slim bash -c '
  set -euo pipefail
  pip install --quiet -U "huggingface_hub[cli]"
  python - <<'PY'
import json, os, shutil, subprocess
stage = "/tmp/hf_stage"
for m in json.loads(os.environ["MANIFEST"]):
    for f in m["files"]:
        dest_dir = os.path.join("/models", f["dest_subdir"])
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, os.path.basename(f["repo_path"]))
        if os.path.exists(dest):
            print("== have", dest, flush=True)
            continue
        print("==>", f["hf_repo"], f["repo_path"], flush=True)
        subprocess.check_call(
            ["hf", "download", f["hf_repo"], f["repo_path"], "--local-dir", stage]
        )
        shutil.copyfile(os.path.join(stage, f["repo_path"]), dest)
shutil.rmtree(stage, ignore_errors=True)
PY
'

# Prune superseded weights: delete any managed weight file NOT named by the FULL catalog
# (every model, not just the ids selected this run). So swapping a model's weights (fp8 ->
# bf16) reclaims the old file, while a model the catalog still references — including one
# provisioned in a separate run, like qwen-image-edit — is never touched. Only weight files
# in catalog-managed subdirs are considered, so a hand-added LoRA or a stray note is safe.
say "Pruning weight files no longer in the catalog"
FULL_MANIFEST="$(catalog -m jbrain.image_gen.catalog)"
MANIFEST="$FULL_MANIFEST" MODELS_DIR="$MODELS_DIR" python3 - <<'PY'
import json, os

manifest = json.loads(os.environ["MANIFEST"])
root = os.environ["MODELS_DIR"]
_WEIGHT_EXTS = (".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin")
# Expected basenames per managed subdir, across the WHOLE catalog.
keep: dict[str, set[str]] = {}
for model in manifest:
    for f in model["files"]:
        keep.setdefault(f["dest_subdir"], set()).add(os.path.basename(f["repo_path"]))
for subdir, names in keep.items():
    directory = os.path.join(root, subdir)
    if not os.path.isdir(directory):
        continue
    for entry in sorted(os.listdir(directory)):
        path = os.path.join(directory, entry)
        if not os.path.isfile(path) or entry in names or not entry.endswith(_WEIGHT_EXTS):
            continue
        print(f"[comfyui] removing orphaned weight {subdir}/{entry}", flush=True)
        os.remove(path)
PY

# The ComfyUI container must join the HOST's video/render group GIDs to open
# /dev/dri/renderD128. Prefer the device's actual owning GID (authoritative); fall
# back to the named groups. Warn if we can't resolve any.
RENDER_GID="$(stat -c %g /dev/dri/renderD128 2>/dev/null || getent group render | cut -d: -f3 || true)"
VIDEO_GID="$(getent group video | cut -d: -f3 || true)"
if [ -z "$RENDER_GID" ] && [ -z "$VIDEO_GID" ]; then
  say "WARNING: could not resolve a render/video GID — ComfyUI may be denied /dev/dri."
fi

# Flip the feature on in .env (idempotent): enabled flag, gateway URL, the selected
# catalog ids as a JSON array, and the GPU GIDs (shared with the local-llm gateway).
# We do NOT persist COMPOSE_PROFILES — the `jbrain` helper activates the comfyui
# profile from COMFYUI_ENABLED so the service isn't dragged into unrelated commands.
COMFYUI_MODELS_JSON="$(printf '%s\n' "${IDS[@]}" | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')"
say "Enabling image generation in .env"
sed -i '/^COMFYUI_ENABLED=/d; /^COMFYUI_URL=/d; /^COMFYUI_MODELS=/d; /^VIDEO_GID=/d; /^RENDER_GID=/d' .env
{
  echo "COMFYUI_ENABLED=true"
  echo "COMFYUI_URL=http://comfyui:8188"
  echo "COMFYUI_MODELS=$COMFYUI_MODELS_JSON"
  [ -n "$VIDEO_GID" ] && echo "VIDEO_GID=$VIDEO_GID"
  [ -n "$RENDER_GID" ] && echo "RENDER_GID=$RENDER_GID"
} >> .env

say "Starting the ComfyUI service"
docker compose --profile comfyui up -d comfyui

say "Done. Image generation is now available to jerv. The first render with a model pays a"
say "one-time load; a 20-step Qwen-Image takes ~3.5 min on the iGPU, while the DreamShaper XL"
say "fast model (generate_image speed: fast) renders in seconds."
