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
  # Host-mode web preview: once enabled (jbrain enable-jcode-preview), keep the base host
  # present across PWA updates too — default to the domain — so it stays turnkey like the
  # keys above. The helper never auto-enables host mode (needs the one-time Cloudflare
  # wildcard first) and is a no-op unless MODE=host is already set.
  sh src/deploy/jcode-preview-backfill.sh .env || echo "[update] jcode preview backfill skipped"
fi

echo "[update] building images"
docker compose $JCODE_PROFILE build

echo "[update] running migrations"
docker compose run --rm migrate

echo "[update] restarting stack"
docker compose $JCODE_PROFILE up -d

# Provision any locally-hosted LLM models the operator queued from the PWA (and
# keep the current + recommended set present). Runs AFTER the stack is up so the
# app stays usable during a long weight download, and the per-model progress bar
# can read on-disk bytes through the live api. Best-effort: a sync failure must
# never abort the update — the queue persists and the next update retries.
echo "[update] syncing local models"
sh src/deploy/local-models-sync.sh || echo "[update] local-model sync skipped (will retry next update)"

# Reclaim space, but never let a prune hiccup fail the whole update (set -e) after
# the real work is done — a transient daemon error here once surfaced as a bogus
# "update failed" with a fully-updated stack.
docker image prune -f || true
docker builder prune -f --keep-storage 10GB >/dev/null 2>&1 || true
echo "[update] complete"
