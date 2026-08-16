#!/bin/sh
# THE update. One implementation, two callers:
#
#   PWA   — the supervisor launches this as a detached one-shot (docker:cli image) so it
#           survives the stack, supervisor included, restarting beneath it. The project
#           dir is mounted at its real host path so compose's relative binds resolve.
#   SSH   — `jbrain update` runs this directly with JBRAIN_HOST_UPDATE=1.
#
# It is one file because it was two, and they drifted. The host copy never rebuilt the
# gateway on the floating llama.cpp tag and never loaded a model to smoke-test it; the
# containerized copy never re-applied the OOM hardening. So the PWA path did the
# memory-heavy thing WITHOUT the protection against exactly that — and hard-locked the
# box (see the gateway-stop note below, which had already recorded this failure once).
# Whatever is decided about an update is now decided once.
#
# The two callers genuinely differ in CAPABILITY, not in intent: mDNS setup, on-box image
# models and OOM hardening need the real host (systemd, sysctls, apt), which the updater
# container has no route to. Those are gated on JBRAIN_HOST_UPDATE and named as such,
# rather than living in a second script that can fall behind.
set -eu

# Set by `jbrain update`; empty in the container. Gates the host-only steps below.
HOST_UPDATE="${JBRAIN_HOST_UPDATE:-}"

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

# LAN access (host-only): turn it on for installs that predate it, then re-provision the
# host mDNS responder + alias from the freshly pulled source. Needs systemd/avahi on the
# real host, so the containerized caller skips it — the setting persists in .env either
# way, and the next host update re-applies it. Best-effort: never aborts an update.
if [ -n "$HOST_UPDATE" ]; then
  if ! grep -q '^JBRAIN_LAN_ADDR=' .env; then
    printf 'JBRAIN_LAN_ADDR=%s\n' "https://jbrain.local" >> .env
  fi
  if grep -q '^JBRAIN_LAN_ADDR=https' .env; then
    JBRAIN_INSTALL_DIR=/opt/jbrain2 sh src/deploy/lan-setup.sh \
      || echo "[update] LAN setup skipped (run 'jbrain enable-lan' to retry)"
  fi
fi

# The opt-in Cloudflare Tunnel connector is profile-gated. Keep it in the build/recreate
# when enabled so an update actually refreshes the connector — the host path already did
# this and the containerized one did not, which meant a PWA update silently left the
# tunnel on its old image.
TUNNEL_PROFILE=""
if grep -q '^TUNNEL_ENABLED=true' .env; then
  TUNNEL_PROFILE="--profile tunnel"
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

  # 1. Ask the gateway to RELEASE its models first. Killing the container alone leaves the
  #    kernel to reclaim tens of gigabytes as the process dies, at exactly the moment the
  #    build and recreate start allocating — which is the race the note above describes.
  #    Unloading first makes that memory go away in a controlled way, before anything
  #    needs it. Best-effort: a gateway already down or holding nothing is a success.
  echo "[update] releasing loaded models before stopping the gateway"
  docker compose run --rm --no-deps -T api python -m jbrain.cli local-llm-unload \
    || echo "[update] unload skipped (gateway unreachable?)"

  # 2. Stop AND REMOVE it. `stop` leaves a container that a stray `up -d` — including the
  #    model sync's own, see JBRAIN_SKIP_GATEWAY_START below — can bring straight back
  #    mid-update, reloading the weights we just released into the middle of the churn.
  #    Removing it means nothing restarts the gateway by accident: it has to be recreated
  #    deliberately, which this script does once, at the end.
  echo "[update] stopping and removing the local-llm gateway"
  docker compose --profile local-llm rm -sf local-llm || true

  # 3. Do not proceed while it is still winding down. Allocating on top of a gateway that
  #    has not finished releasing is the whole failure mode; a few seconds of waiting is
  #    cheap next to a hard-locked host. Bounded, and only a warning if it overruns — the
  #    update must not hang forever on a wedged container.
  _waited=0
  while [ "$_waited" -lt 60 ]; do
    if [ -z "$(docker compose --profile local-llm ps -q local-llm 2>/dev/null)" ]; then
      break
    fi
    sleep 2
    _waited=$((_waited + 2))
  done
  [ "$_waited" -lt 60 ] \
    || echo "[update] WARNING: gateway still present after ${_waited}s — continuing anyway"
  echo "[update] gateway down after ${_waited}s; memory released"
fi

# Nothing may restart the gateway between here and the deliberate restart at the end.
# local-models-sync.sh brings it up itself when the roster changed, which lands squarely in
# the middle of the build and recreate — the one window this whole dance exists to keep
# clear. It honours this flag and leaves the restart to us.
JBRAIN_SKIP_GATEWAY_START=1
export JBRAIN_SKIP_GATEWAY_START

# Stamp the image with the exact source revision being built so the running server
# can report what is deployed (debug /version) — no more guessing whether a merge is
# live. Computed from the just-reset `src` mirror (safe.directory was marked above);
# a fetch/build failure would have aborted before here, so these describe real HEAD.
JBRAIN_GIT_SHA="$(git -C src rev-parse HEAD 2>/dev/null || echo unknown)"
JBRAIN_GIT_DESCRIBE="$(git -C src describe --tags --always --dirty 2>/dev/null || echo unknown)"
JBRAIN_BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
export JBRAIN_GIT_SHA JBRAIN_GIT_DESCRIBE JBRAIN_BUILD_TIME

# Persist them into .env as well as exporting them. compose reads .env automatically, so
# the stamp becomes a property of the DEPLOYMENT DIRECTORY rather than of this one script's
# process environment. Exporting alone means any later `docker compose build` run outside
# this script — a per-service rebuild from Ops, a hand-run recreate — resolves
# `${JBRAIN_GIT_SHA:-unknown}` to literally "unknown" and bakes an image that cannot say
# which revision it is. That is precisely the question /api/debug/version exists to answer,
# and it was observed answering "unknown" on a box whose deploy history had the real sha.
for _stamp in "JBRAIN_GIT_SHA=$JBRAIN_GIT_SHA" \
              "JBRAIN_GIT_DESCRIBE=$JBRAIN_GIT_DESCRIBE" \
              "JBRAIN_BUILD_TIME=$JBRAIN_BUILD_TIME"; do
  _key="${_stamp%%=*}"
  # Rewrite in place when present, append when not — never accumulate duplicate keys, which
  # would leave the LAST write winning by accident of ordering.
  if grep -q "^${_key}=" .env; then
    sed -i "s|^${_key}=.*|${_stamp}|" .env
  else
    printf '%s\n' "$_stamp" >> .env
  fi
done
echo "[update] building images (rev $JBRAIN_GIT_DESCRIBE)"
docker compose $JCODE_PROFILE $TUNNEL_PROFILE build

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
docker compose $JCODE_PROFILE $TUNNEL_PROFILE up -d

# Provision any locally-hosted LLM models the operator queued from the PWA (and
# keep the current + recommended set present). Runs AFTER the stack is up so the
# app stays usable during a long weight download, and the per-model progress bar
# can read on-disk bytes through the live api. Best-effort: a sync failure must
# never abort the update — the queue persists and the next update retries.
echo "[update] syncing local models"
sh src/deploy/local-models-sync.sh || echo "[update] local-model sync skipped (will retry next update)"

# On-box image generation (host-only): re-sync its models so an update that adds newly
# recommended ones pulls them with no separate manual step. We provision the UNION of the
# operator's selection and the recommended set, so nothing they chose is dropped. Needs the
# host installer, so the containerized caller skips it. Best-effort.
if [ -n "$HOST_UPDATE" ] && grep -q '^COMFYUI_ENABLED=true' .env; then
  _current="$(python3 -c "import json,re; t=open('.env').read(); m=re.search(r'^COMFYUI_MODELS=(.*)$',t,re.M); print(' '.join(json.loads(m.group(1))) if m else '')" 2>/dev/null || true)"
  _reco="$(docker compose run --rm --no-deps -T api python -c "from jbrain.image_gen import catalog; print(' '.join(catalog.recommended_ids()))" 2>/dev/null || true)"
  _ids="$(printf '%s\n%s\n' "$_current" "$_reco" | tr ' ' '\n' | grep -v '^[[:space:]]*$' | sort -u | tr '\n' ' ')"
  if [ -n "$_ids" ]; then
    echo "[update] syncing on-box image models: $_ids"
    JBRAIN_INSTALL_DIR=/opt/jbrain2 sh src/scripts/comfyui-setup.sh $_ids \
      || echo "[update] image-model sync skipped (retry: sudo bash scripts/comfyui-setup.sh)"
  fi
fi

# OOM hardening (host-only): earlyoom + reclaim-headroom sysctls, re-applied so a box that
# predates the automated hardening — or lost it — is protected on its next update. This is
# the protection against the reclaim livelock described above, which makes its absence from
# the containerized path the single worst consequence of the two scripts having drifted:
# that path is the one that rebuilds the gateway and loads a model. It still cannot run
# here (sysctls and apt need the real host), so a PWA update relies on hardening a previous
# host update applied. That is a real remaining gap, not a solved one.
if [ -n "$HOST_UPDATE" ] && grep -q '^LOCAL_LLM_ENABLED=true' .env; then
  sh src/deploy/oom-hardening.sh || echo "[update] OOM hardening skipped (retry next update)"
fi

# Bring the LLM gateway back up now the churn is over (it loads models on demand).
# Only when it was up before this update: the model sync above restarts it when the
# roster CHANGED, but the common no-op sync ("no models to sync") exits before its
# own `up -d`, so without this an enabled gateway would stay stopped after every
# routine update. Idempotent with the sync's own start; best-effort like the rest.
if [ -n "$LOCAL_LLM_RUNNING" ]; then
  echo "[update] restarting local-llm gateway"
  docker compose --profile local-llm up -d local-llm || true
fi

# DEFAULT-ON: track the newest llama.cpp on the gateway so a freshly-released model's
# architecture is supported WITHOUT any manual step — the owner drives this box from the
# PWA with no terminal (CLAUDE.md #10), so "edit .env / re-run a command" is not a path
# they have. Every update rebuilds the gateway on the FLOATING base tag (default kyuz0
# :vulkan-radv, which tracks llama.cpp master; override with LOCAL_LLM_BASE_FLOATING, e.g.
# a rocm-* tag) with --pull, then smoke-tests it (jbrain.cli local-llm-smoketest: load a
# model + a gpt-oss tool probe). If the new build can't load a model or breaks tool calls,
# roll BACK to the pinned, known-good LOCAL_LLM_BASE so a bad upstream build never leaves
# the box unable to serve — the rollback net is what makes tracking master safe by default.
# Opt OUT with LOCAL_LLM_AUTO_UPDATE=false to freeze the reproducible pinned digest (see
# docs/runbooks/STRIX_HALO_SETUP.md, "Reproducibility / trust"). Gated on LOCAL_LLM_RUNNING
# (so LOCAL_LLM_ENABLED=true), and best-effort: every branch is guarded so a hiccup never
# aborts the update (set -e).
# The owner's PWA toggle (Settings), read through the api image. `.env` still wins when it
# says false, so an existing opt-out keeps working; otherwise the stored setting decides.
# This is the CLAUDE.md #10 fix for this switch: it governs whether an update loads a model
# into the GPU at all, and it used to be reachable only by editing .env on the host.
AUTO_UPDATE_ON=1
if grep -q '^LOCAL_LLM_AUTO_UPDATE=false' .env; then
  AUTO_UPDATE_ON=''
elif ! docker compose run --rm --no-deps -T api python -m jbrain.cli local-llm-auto-update; then
  echo "[update] gateway auto-update is OFF (Settings) — keeping the pinned base, no model load"
  AUTO_UPDATE_ON=''
fi

if [ -n "$LOCAL_LLM_RUNNING" ] && [ -n "$AUTO_UPDATE_ON" ]; then
  FLOATING="$(sed -n 's/^LOCAL_LLM_BASE_FLOATING=//p' .env | tail -n1)"
  [ -n "$FLOATING" ] || FLOATING="docker.io/kyuz0/amd-strix-halo-toolboxes:vulkan-radv"
  echo "[update] LOCAL_LLM_AUTO_UPDATE: rebuilding gateway on newest llama.cpp ($FLOATING)"
  # The image id BEFORE the rebuild. The smoke test exists to catch a bad UPSTREAM build,
  # so when the rebuild produces the identical image there is nothing new to vet — and the
  # test is not free: it loads a model into the iGPU, which is tens of GB of disk read and
  # minutes of the box's attention, on every routine update that changed nothing. Skipping
  # the unchanged case is what stops a no-op update from pinning the GPU.
  BEFORE_IMG="$(docker image inspect -f '{{.Id}}' jbrain2-local-llm:local 2>/dev/null || true)"
  if LOCAL_LLM_BASE="$FLOATING" docker compose --profile local-llm build --pull local-llm \
      && docker compose --profile local-llm up -d local-llm; then
    AFTER_IMG="$(docker image inspect -f '{{.Id}}' jbrain2-local-llm:local 2>/dev/null || true)"
    if [ -n "$BEFORE_IMG" ] && [ "$BEFORE_IMG" = "$AFTER_IMG" ]; then
      echo "[update] gateway image unchanged — skipping the smoke test (nothing new to vet)"
    elif docker compose run --rm --no-deps -T api python -m jbrain.cli local-llm-smoketest; then
      echo "[update] gateway smoke test passed on the newest llama.cpp"
    else
      SMOKE_FAILED=1
    fi
  else
    SMOKE_FAILED=1
  fi
  if [ -n "${SMOKE_FAILED:-}" ]; then
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
