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
# Remove any stale container of this name FIRST. A previous run that was killed
# uncleanly (the update one-shot dying mid-download) leaves the named container
# behind despite --rm and the trap, and then every retry fails instantly with
# "container name already in use" — no network, no progress. Pre-clean so a resume
# is never blocked by the corpse of the run it's resuming.
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
# shellcheck disable=SC2086  # TTY_FLAG is an intentional optional word.
docker run --rm $TTY_FLAG --name "$CONTAINER" \
  -e MANIFEST="$MANIFEST" -v "$MODELS_DIR:/models" python:3.11-slim bash -c '
  set -euo pipefail
  pip install --quiet -U huggingface_hub
  # The heredoc delimiter is QUOTED so the Python below is fed verbatim. With an
  # unquoted delimiter bash would command-substitute backticks in the body (a
  # backticked hf-download mention in a comment actually ran the command and
  # injected its help text into the source, breaking the parse) and expand any
  # dollar sign. Python reads MANIFEST from the environment at runtime, so it needs
  # no shell expansion here. The quoted delimiter is written '"'"'PY'"'"' to survive
  # this outer single-quoted bash -c string.
  python - <<'"'"'PY'"'"'
import json, os, subprocess, time
import huggingface_hub

def _bytes(p):
    total = 0
    for dp, _d, fs in os.walk(p):
        for f in fs:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total

# Name the tool + version up front — a chat-template / arch mismatch or a resume bug
# is often a stale hub, so the reader wants this pinned in the log.
print(f"== huggingface_hub {huggingface_hub.__version__} ==", flush=True)

for m in json.loads(os.environ["MANIFEST"]):
    mid = m["id"]
    dest = f"/models/{mid}"
    includes = [m["gguf_include"]] + ([m["mmproj_include"]] if m["mmproj_include"] else [])
    args = ["hf", "download", m["hf_repo"], "--local-dir", dest]
    for inc in includes:
        args += ["--include", inc]
    print("==>", " ".join(args), flush=True)
    # Progress-aware resume. A 100 GB pull over a flaky link can reset its
    # connection every few hundred MB; `hf download` resumes from the .incomplete
    # partials each attempt. A FIXED retry count gives up mid-download (5 retries
    # buys only ~3 GB if it drops every ~0.6 GB), so instead keep going as long as
    # each attempt grows the on-disk size, and bail only after several attempts
    # that make NO progress — a genuinely stuck failure, not a slow one.
    stuck = 0
    while True:
        before = _bytes(dest)
        try:
            subprocess.check_call(args)
            break
        except subprocess.CalledProcessError as exc:
            gained = _bytes(dest) - before
            if gained > 0:
                stuck = 0
                print(f"== connection dropped; resuming (+{gained // (1024 * 1024)} MB this pass) ==", flush=True)
            else:
                stuck += 1
                print(f"== download failed with no progress ({stuck}/5) ==", flush=True)
                if stuck >= 5:
                    # A loud, greppable terminal marker: the reason (hf stderr) is
                    # already streamed above; this pins WHICH model died, the hf exit
                    # code, and what (if anything) landed on disk, for the log tail the
                    # PWA and /api/debug/provision/status read.
                    listing = sorted(os.listdir(dest)) if os.path.isdir(dest) else "MISSING"
                    print(f"== MODEL {mid} FAILED: {m['hf_repo']} — hf exited {exc.returncode} after {stuck} attempts with no progress ==", flush=True)
                    print(f"== {mid} dest {dest} contains: {listing} ==", flush=True)
                    raise
            time.sleep(min(15, 3 * (stuck + 1)))
PY
'
