#!/bin/sh
# Shared GGUF weight downloader for the two local-model provisioning paths:
# scripts/local-llm-setup.sh (first enable, on the host) and
# deploy/local-models-sync.sh (the update one-shot's sync, in the bash-less
# docker:cli container). POSIX sh so it runs in BOTH.
#
# Reads the catalog MANIFEST (a JSON array of LocalModel dicts) from $MANIFEST
# and pulls each model's --include globs into <models_dir>/<id>/ with the
# official huggingface_hub CLI in a throwaway python:3.11-slim container.
# Idempotent: huggingface skips files already present, so an unchanged set is a
# cheap no-op and a partial download resumes.
#
#   MANIFEST="$json" sh download-local-weights.sh <models_dir>
set -eu

MODELS_DIR="${1:?usage: download-local-weights.sh <models_dir>}"
: "${MANIFEST:?MANIFEST env (catalog JSON) is required}"
# Stable container name so we can force-remove it on any exit; the caller may
# override to share its own cleanup name.
CONTAINER="${DOWNLOAD_CONTAINER:-jbrain-local-llm-download}"

# Force-remove the named downloader on ANY exit so a Ctrl+C can't strand it
# still holding the HF download lock for the next run.
trap 'docker rm -f "$CONTAINER" >/dev/null 2>&1 || true' EXIT INT TERM

# Allocate a TTY when we have one so huggingface_hub renders live per-file
# percentage bars (size, speed, ETA); without a TTY it collapses to a terse
# file counter — still followable in the update log.
TTY_FLAG=""
[ -t 1 ] && TTY_FLAG="-t"

mkdir -p "$MODELS_DIR"
# shellcheck disable=SC2086  # TTY_FLAG is an intentional optional word.
docker run --rm $TTY_FLAG --name "$CONTAINER" \
  -e MANIFEST="$MANIFEST" -v "$MODELS_DIR:/models" python:3.11-slim bash -c '
  set -euo pipefail
  pip install --quiet -U "huggingface_hub[cli]"
  python - <<PY
import json, os, subprocess
for m in json.loads(os.environ["MANIFEST"]):
    mid = m["id"]
    dest = f"/models/{mid}"
    includes = [m["gguf_include"]] + ([m["mmproj_include"]] if m["mmproj_include"] else [])
    args = ["hf", "download", m["hf_repo"], "--local-dir", dest]
    for inc in includes:
        args += ["--include", inc]
    print("==>", " ".join(args), flush=True)
    subprocess.check_call(args)
PY
'
