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
import json, os, signal, subprocess, time
import huggingface_hub

# Kill and resume a transfer that writes NOTHING for this long. A silently hung
# connection (never errors, never returns) would make a plain check_call block
# forever — the exact stall that stranded a 120B pull with the updater stuck
# "running" and zero bytes moving. A live download writes continuously, so this
# only ever trips on a genuine hang, not a slow link.
STALL_SECONDS = 180

def _bytes(p):
    total = 0
    for dp, _d, fs in os.walk(p):
        for f in fs:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total

def _run_until_stalled(args, dest):
    # Run `hf download`, but kill it (and its process group) if the on-disk size
    # stops growing for STALL_SECONDS, returning a nonzero code so the retry loop
    # resumes from the .incomplete partials. Output inherits our stdout, so the hf
    # progress still streams to the provision log. start_new_session puts hf in its
    # own group so a hung child is killed with it.
    proc = subprocess.Popen(args, start_new_session=True)
    last_size = _bytes(dest)
    last_change = time.monotonic()
    while True:
        try:
            return proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
        size = _bytes(dest)
        if size > last_size:
            last_size = size
            last_change = time.monotonic()
        elif time.monotonic() - last_change >= STALL_SECONDS:
            print(f"== no bytes for {STALL_SECONDS}s — killing stalled transfer to resume ==", flush=True)
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except OSError:
                    proc.kill()
                try:
                    return proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    continue
            return proc.wait()

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
        rc = _run_until_stalled(args, dest)
        if rc == 0:
            break
        # hf exited nonzero, OR the watchdog killed a hung transfer. Same recovery
        # either way: resume from the .incomplete partials. Distinguish a slow-but-
        # moving link (progress this pass) from a genuinely stuck one (none) so the
        # retry budget bounds only real failures, not a flaky 100 GB pull.
        gained = _bytes(dest) - before
        if gained > 0:
            stuck = 0
            print(f"== transfer interrupted; resuming (+{gained // (1024 * 1024)} MB this pass) ==", flush=True)
        else:
            stuck += 1
            print(f"== download failed with no progress ({stuck}/5) ==", flush=True)
            if stuck >= 5:
                # A loud, greppable terminal marker: the reason (hf stderr) is
                # already streamed above; this pins WHICH model died, the hf exit
                # code, and what (if anything) landed on disk, for the log tail the
                # PWA and /api/debug/provision/status read.
                listing = sorted(os.listdir(dest)) if os.path.isdir(dest) else "MISSING"
                print(f"== MODEL {mid} FAILED: {m['hf_repo']} — hf exited {rc} after {stuck} attempts with no progress ==", flush=True)
                print(f"== {mid} dest {dest} contains: {listing} ==", flush=True)
                raise SystemExit(1)
            time.sleep(min(15, 3 * (stuck + 1)))
PY
'
