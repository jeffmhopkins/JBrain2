#!/bin/sh
# Containerized `jbrain update`, launched by the supervisor as a detached
# one-shot (docker:cli image) so it survives the stack — including the
# supervisor itself — restarting beneath it. The project dir is mounted at
# its real host path, so compose's relative bind paths resolve correctly.
set -eu

echo "[update] starting"
./backup.sh || echo "[update] backup skipped (stack not fully up?)"

echo "[update] pulling latest main"
# The pull runs as root inside the ephemeral updater container, but the
# bind-mounted worktree is owned by the host operator's UID, so git's
# dubious-ownership guard aborts ("detected dubious ownership"). Mark the
# worktree safe for the container's root user — a host-side `safe.directory`
# never reaches here, since root's container HOME carries no gitconfig.
git config --global --add safe.directory "$PWD/src"
# Mirror the remote exactly rather than `pull --ff-only`: a deploy box should never
# diverge, but if it has (a stray commit/edit), ff-only refuses and aborts the update,
# pinning the stack to stale source. fetch + hard reset to the tracked upstream
# self-heals — discarding local src changes by design, since src is a pristine mirror.
git -C src fetch origin
git -C src reset --hard "@{u}"

# Refresh host helper scripts from the updated tree (mv keeps any running
# reader on its old inode).
for f in docker-compose.yml backup.sh restore.sh jbrain; do
  cp "src/deploy/$f" "$f.new" && mv "$f.new" "$f"
done
cp src/deploy/db-init/01-app-role.sh db-init/
chmod +x jbrain backup.sh restore.sh db-init/01-app-role.sh

# Refresh the SearXNG settings host file. Compose bind-mounts it writable (the
# image injects $SEARXNG_SECRET at boot) and it enables the JSON format the
# web_search tool needs. Deployments that predate this service have no such file,
# so the bind source is missing, Docker mounts an empty dir over it, SearXNG
# falls back to its HTML-only defaults, and /search?format=json answers 403 —
# jerv then reports web search as unavailable. rm first: on a box already broken
# this way the path is that Docker-made directory, and `cp file dir/` would drop
# the file inside it rather than replace it, leaving the dir mount in place.
mkdir -p searxng
rm -rf searxng/settings.yml
cp src/deploy/searxng/settings.yml searxng/settings.yml

# Backfill SEARXNG_SECRET for stacks updated from before the web-search service:
# SearXNG refuses to start without one. busybox has no openssl, so derive the hex
# from /dev/urandom. Append only when absent so an existing secret stands.
if ! grep -q '^SEARXNG_SECRET=' .env; then
  echo "[update] adding SEARXNG_SECRET for web search"
  printf 'SEARXNG_SECRET=%s\n' "$(head -c 32 /dev/urandom | sha256sum | cut -d' ' -f1)" >> .env
fi

# Code mode (jcode): an opt-in, profile-gated coding sandbox. When the operator has
# enabled it (a deliberate one-time scripts/jcode-setup.sh), fold it into the PWA
# update so it is rebuilt, recreated, and kept current with NO CLI — and self-heal
# its .env keys (mint the api<->jcode bearer + fail-closed defaults) so an update
# never needs a jcode-setup.sh re-run. Compose maps these bare keys to the api
# (JBRAIN_JCODE_*) and the sandbox (JCODE_*). Disabled => empty profile => the
# sandbox stays absent on a stock stack. ..* requires a non-empty value (busybox BRE).
JCODE_PROFILE=""
if grep -q '^JCODE_ENABLED=true' .env; then
  JCODE_PROFILE="--profile jcode"
  if ! grep -q '^JCODE_TOKEN=..*' .env; then
    echo "[update] minting JCODE_TOKEN (api<->jcode bearer)"
    printf 'JCODE_TOKEN=%s\n' "$(head -c 32 /dev/urandom | sha256sum | cut -d' ' -f1)" >> .env
  fi
  grep -q '^JCODE_URL=' .env || printf 'JCODE_URL=%s\n' 'http://jcode:9100' >> .env
  grep -q '^JCODE_MODEL=' .env || printf 'JCODE_MODEL=%s\n' 'qwen3-coder-next' >> .env
  grep -q '^JCODE_MODEL_URL=' .env || printf 'JCODE_MODEL_URL=%s\n' 'http://local-llm:8080' >> .env
fi

# Job launcher (jlaunch): DEFAULT-ON (single-user box) — part of the base stack, not
# profile-gated, so the plain build/recreate below already include it. Always backfill the
# api<->jlaunch bearer for boxes that predate it (the fail-closed control server won't start
# without it); the URL is defaulted in compose.
if ! grep -q '^JLAUNCH_TOKEN=..*' .env; then
  echo "[update] minting JLAUNCH_TOKEN (api<->jlaunch bearer)"
  printf 'JLAUNCH_TOKEN=%s\n' "$(head -c 32 /dev/urandom | sha256sum | cut -d' ' -f1)" >> .env
fi

# Read-aloud (server-side Kokoro TTS) needs NOTHING here: Kokoro AND its weights
# are baked into the tts-stt image (deploy/Dockerfile.tts-stt), rebuilt
# by the `docker compose build` below. It is driven entirely by the Settings toggle
# (brain_read_aloud) at runtime — no env var, no host download, no provisioning step.

# Free the on-box LLM gateway's memory BEFORE the rebuild + recreate. The gateway
# keeps its resident model set (~91 GB on the Strix Halo box) pinned in unified
# memory, and it is profile-gated — the plain `up -d` below never recreates it, so
# without this it sits at full memory through the whole update. Recreating every
# other container on top of that once spiked allocation into a kernel reclaim
# livelock that hard-locked the host (even USB keyboard/mouse), forcing a power
# cycle. Stopping it here releases the memory for the build/migrate/recreate; it is
# brought back after the stack is up (below). Gated on hosting being enabled so a
# stock cloud stack is untouched; best-effort so a stop hiccup never fails the update.
LOCAL_LLM_RUNNING=""
if grep -q '^LOCAL_LLM_ENABLED=true' .env; then
  LOCAL_LLM_RUNNING=1
  echo "[update] stopping local-llm gateway to free memory for the update"
  docker compose --profile local-llm stop local-llm || true
fi

# Stamp the image with the exact source revision being built so the running server
# can report what is deployed (debug /version) — no more guessing whether a merge is
# live. Computed from the just-reset `src` mirror (safe.directory was marked above);
# a fetch/build failure would have aborted before here, so these describe real HEAD.
JBRAIN_GIT_SHA="$(git -C src rev-parse HEAD 2>/dev/null || echo unknown)"
JBRAIN_GIT_DESCRIBE="$(git -C src describe --tags --always --dirty 2>/dev/null || echo unknown)"
JBRAIN_BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
export JBRAIN_GIT_SHA JBRAIN_GIT_DESCRIBE JBRAIN_BUILD_TIME
echo "[update] building images (rev $JBRAIN_GIT_DESCRIBE)"
docker compose $JCODE_PROFILE build

echo "[update] running migrations"
docker compose run --rm migrate

# Clear containers left behind by a renamed (server-brain->wall, whisper->tts-stt) or removed
# (claude-shim) service before the `up -d` below — an old server-brain holding host port 8800
# blocks the new `wall` (which is what took the tunnel down after the split update), and any such
# labeled orphan lingers on the Ops screen. The helper sweeps by comparing labels against the
# file's full service set, so profile-gated services `--remove-orphans` would wrongly reap are kept.
echo "[update] clearing renamed/removed-service orphans"
sh src/deploy/prune-orphans.sh || echo "[update] orphan sweep skipped"

echo "[update] restarting stack"
docker compose $JCODE_PROFILE up -d

# Provision any locally-hosted LLM models the operator queued from the PWA (and
# keep the current + recommended set present). Runs AFTER the stack is up so the
# app stays usable during a long weight download, and the per-model progress bar
# can read on-disk bytes through the live api. Best-effort: a sync failure must
# never abort the update — the queue persists and the next update retries.
echo "[update] syncing local models"
sh src/deploy/local-models-sync.sh || echo "[update] local-model sync skipped (will retry next update)"

# Bring the LLM gateway back up now the churn is over (it loads models on demand).
# Only when it was up before this update: the model sync above restarts it when the
# roster CHANGED, but the common no-op sync ("no models to sync") exits before its
# own `up -d`, so without this an enabled gateway would stay stopped after every
# routine update. Idempotent with the sync's own start; best-effort like the rest.
if [ -n "$LOCAL_LLM_RUNNING" ]; then
  echo "[update] restarting local-llm gateway"
  docker compose --profile local-llm up -d local-llm || true
fi

# OPT-IN: track the newest llama.cpp on the gateway so a freshly-released model's
# architecture is supported without a manual digest bump. LOCAL_LLM_AUTO_UPDATE=true
# rebuilds the gateway on the FLOATING base tag (default kyuz0 :vulkan-radv, which tracks
# llama.cpp master; override with LOCAL_LLM_BASE_FLOATING, e.g. a rocm-* tag) with --pull,
# then smoke-tests it (jbrain.cli local-llm-smoketest: load a model + a gpt-oss tool probe).
# If the new build can't load a model or breaks tool calls, roll BACK to the pinned,
# known-good LOCAL_LLM_BASE so a bad upstream build never leaves the box unable to serve.
# Absent/false keeps the reproducible pinned digest — see docs/runbooks/STRIX_HALO_SETUP.md
# ("Reproducibility / trust"). Gated on LOCAL_LLM_RUNNING (so LOCAL_LLM_ENABLED=true), and
# best-effort: every branch is guarded so a hiccup never aborts the update (set -e).
if [ -n "$LOCAL_LLM_RUNNING" ] && grep -q '^LOCAL_LLM_AUTO_UPDATE=true' .env; then
  FLOATING="$(sed -n 's/^LOCAL_LLM_BASE_FLOATING=//p' .env | tail -n1)"
  [ -n "$FLOATING" ] || FLOATING="docker.io/kyuz0/amd-strix-halo-toolboxes:vulkan-radv"
  echo "[update] LOCAL_LLM_AUTO_UPDATE: rebuilding gateway on newest llama.cpp ($FLOATING)"
  if LOCAL_LLM_BASE="$FLOATING" docker compose --profile local-llm build --pull local-llm \
      && docker compose --profile local-llm up -d local-llm \
      && docker compose run --rm --no-deps -T api python -m jbrain.cli local-llm-smoketest; then
    echo "[update] gateway smoke test passed on the newest llama.cpp"
  else
    echo "[update] WARNING: newest llama.cpp failed the smoke test — rolling back to the pinned base"
    # No LOCAL_LLM_BASE override and no --pull: rebuild against the reproducible pinned
    # digest (compose default or the operator's .env value) from cached layers.
    docker compose --profile local-llm build local-llm \
      && docker compose --profile local-llm up -d local-llm \
      || echo "[update] WARNING: gateway rollback rebuild failed — check 'jbrain logs local-llm'"
  fi
fi

# Reclaim space, but never let a prune hiccup fail the whole update (set -e) after
# the real work is done — a transient daemon error here once surfaced as a bogus
# "update failed" with a fully-updated stack.
docker image prune -f || true
docker builder prune -f --keep-storage 10GB >/dev/null 2>&1 || true
echo "[update] complete"
