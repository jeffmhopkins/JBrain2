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

# Download weights with the official huggingface_hub CLI in a throwaway
# container; only the --include globs from the manifest are pulled.
say "Downloading weights into $MODELS_DIR (this can take a while)"
echo "$MANIFEST" | docker run --rm -i -v "$MODELS_DIR:/models" python:3.11-slim bash -c '
  set -euo pipefail
  pip install --quiet -U "huggingface_hub[cli]"
  python - <<PY
import json, subprocess, sys
for m in json.load(sys.stdin):
    dest = f"/models/{m[\"id\"]}"
    includes = [m["gguf_include"]] + ([m["mmproj_include"]] if m["mmproj_include"] else [])
    args = ["hf", "download", m["hf_repo"], "--local-dir", dest]
    for inc in includes:
        args += ["--include", inc]
    print("==>", " ".join(args), flush=True)
    subprocess.check_call(args)
PY
'

# Generate the llama-swap config on the HOST (it can see the downloaded files;
# the api container can't). Resolve each manifest glob to the real filename —
# and for multi-shard GGUFs pass the first shard, which llama.cpp follows to the
# rest. Paths are the gateway-container view (./local-models is mounted at
# /models). `${PORT}` is llama-swap's per-model upstream-port macro.
say "Writing $MODELS_DIR/llama-swap.yaml"
MANIFEST="$MANIFEST" MODELS_DIR="$MODELS_DIR" python3 <<'PY'
import glob, json, os, sys

models = json.loads(os.environ["MANIFEST"])
root = os.environ["MODELS_DIR"]


def resolve(model_id: str, pattern: str) -> str:
    # Top-level only: `hf download --local-dir` flattens the real files here, so
    # this never matches the hash-named blobs under .cache/.
    matches = sorted(glob.glob(os.path.join(root, model_id, pattern)))
    if not matches:
        sys.exit(f"no file matching {pattern!r} for {model_id} under {root} — download incomplete?")
    shards = [os.path.basename(m) for m in matches if "-00001-of-" in os.path.basename(m)]
    if shards:
        # Multi-part GGUF: verify every shard arrived so we don't hand llama.cpp a
        # partial set that fails cryptically at load time.
        first = shards[0]
        total = int(first.split("-of-")[1].split(".gguf")[0])
        if len(matches) != total:
            sys.exit(f"{model_id}: expected {total} shards for {pattern!r}, found {len(matches)}")
        return first
    return os.path.basename(matches[0])


lines = ["# Generated by scripts/local-llm-setup.sh — do not edit by hand.", "models:"]
for m in models:
    gguf = resolve(m["id"], m["gguf_include"])
    cmd = [
        "/usr/local/bin/llama-server", "--host", "0.0.0.0", "--port", "${PORT}",
        "-m", f"/models/{m['id']}/{gguf}", "-ngl", "999",
    ]
    if m["mmproj_include"]:
        cmd += ["--mmproj", f"/models/{m['id']}/{resolve(m['id'], m['mmproj_include'])}"]
    lines.append(f"  {m['served_model']}:")
    lines.append("    cmd: >")
    lines.append("      " + " ".join(cmd))
with open(os.path.join(root, "llama-swap.yaml"), "w") as f:
    f.write("\n".join(lines) + "\n")
print("resolved", len(models), "model(s)")
PY

# The gateway container must join the HOST's video/render group GIDs to open
# /dev/dri/renderD128. Prefer the device's actual owning GID (authoritative);
# fall back to the named groups. Warn if we can't resolve any — the compose
# name fallback then relies on the groups created in the image, which won't match
# host device ownership and will likely fail to open the GPU.
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
