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
# The two callers genuinely differ in CAPABILITY, not in intent: mDNS setup and on-box
# image models need the real host (systemd, apt), which the updater container has no
# route to. Those are gated on JBRAIN_HOST_UPDATE and named as such, rather than living
# in a second script that can fall behind.
#
# The phase order is deliberate, and it is the answer to this box hard-locking mid-update
# (see the memory section below for the kernel evidence). Nothing here needs to be true at
# the same time as everything else, so it isn't:
#
#   1. backup, source refresh, .env backfills      — stack up, cheap
#   2. heal a stale quiesce, sweep renamed-service orphans, empty and REMOVE the gateway
#   3. QUIESCE: stop everything but the control plane (db/api/supervisor/proxy/cloudflared)
#   4. reclaim hardening, image build              — quiesced
#   5. pause the api too, rebuild the gateway, drop caches, smoke test, empty it again
#      — the smoke test loads the SMALLEST installed model (~1 GB) and probes that same
#      model, so this phase no longer allocates tens of gigabytes at all
#   6. migrate, recreate the stack, restore everything the quiesce stopped
#   7. model syncs, gateway restart, prune         — stack up
#
# The single rule behind it: never allocate tens of gigabytes while also recreating a
# dozen containers.
set -eu

# Set by `jbrain update`; empty in the container. Gates the host-only steps below.
HOST_UPDATE="${JBRAIN_HOST_UPDATE:-}"

# How long a one-off `compose run` may take before the update gives up on it.
#
# These are the calls that have actually wedged this box. An update stalled twice at
# `jbrain-api-run-… Created` — a container that compose created and never started — and
# `set -e` plus an unbounded wait means the update simply stops there forever, stack half
# recreated, with nothing timing out and nothing to report. The gateway client has its own
# httpx timeouts, but they cannot help when the process never starts, and a task stuck in
# an uninterruptible wait does not honour them either. The bound has to be outside.
#
# The smoke test genuinely can be slow — a cold load reads tens of GB — so it gets a
# generous ceiling. The toggle read is one small query and should answer in seconds.
SMOKE_TIMEOUT_S=600
TOGGLE_TIMEOUT_S=120
# The model release gets its OWN ceiling, not the toggle's. `local-llm-unload` walks the
# resident models SEQUENTIALLY, and each unload now waits up to 30 s on the client (llama-swap
# grants its child a 10 s graceful stop, so 3 s used to abandon a stop that was still working).
# Four resident models therefore reach 120 s exactly — the toggle ceiling — and being killed
# there is the worst outcome available: `release_models` swallows the failure, the update
# force-recreates the gateway with tens of GB still pinned, and that is the reclaim storm this
# whole step exists to prevent. Sized for a pathological release instead, since the cost of
# being generous is a slow update and the cost of being tight is a hard-locked host.
UNLOAD_TIMEOUT_S=300
# The floating gateway rebuild pulls a multi-GB base image on a rolling tag. Generous, but
# not unbounded: it runs with the stack quiesced, so a stall here is a stall of the box.
PULL_TIMEOUT_S=1800

# Run a one-off compose container under a hard ceiling, and clean up after a kill.
#
# `timeout` kills `docker compose run`, but the CONTAINER it spawned is not `docker
# compose run`'s child — it belongs to the daemon, so --rm never fires and the container is
# left behind. Left uncollected those accumulate one per attempt, and the next update's
# `compose run` can queue behind them. Sweeping the service's stale one-offs afterwards
# keeps a timeout from becoming a slow leak.
#
# Returns the command's exit status, or 124 when the ceiling fired — callers treat any
# non-zero as failure, so a hang degrades to the same path as a genuine failure.
run_bounded() {
  _limit="$1"
  shift
  # if/else rather than `cmd; rc=$?`: under `set -e` a failing command aborts the script
  # before the assignment runs. It only survives today because every caller uses it where
  # -e is suspended (an `if` condition or the left side of `||`) — which is exactly the
  # kind of thing that breaks the moment someone calls it plainly.
  if timeout "$_limit" "$@"; then
    return 0
  else
    # Captured HERE, in the else. After a completed `if ...; fi`, `$?` is the status of the
    # IF STATEMENT — 0 when the condition simply failed — so reading it below the `fi`
    # silently turns every failure into a success. It did exactly that when first written.
    _rc=$?
  fi
  # GNU timeout reports 124; busybox's has historically exited 143 (128+SIGTERM) instead,
  # and this runs under busybox in the updater image. Treat either as the ceiling firing —
  # getting it wrong only costs the cleanup and the log line, but both matter here.
  if [ "$_rc" -eq 124 ] || [ "$_rc" -eq 143 ]; then
    echo "[update] TIMEOUT after ${_limit}s: $*"
    # The container is the DAEMON's child, not `compose run`'s, so killing compose leaves it
    # behind and --rm never fires. Uncollected they pile up one per attempt.
    for _cid in $(docker ps -aq --filter "name=jbrain-api-run-" 2>/dev/null); do
      docker rm -f "$_cid" >/dev/null 2>&1 || true
    done
  fi
  return "$_rc"
}

# --- memory: measuring it, and actually making room --------------------------
#
# Everything in this section exists because of ONE failure mode, which has hard-locked
# this box repeatedly during updates. The iGPU has no VRAM: a model's device buffers are
# pinned out of unified system RAM through the amdgpu GTT, and the driver requests them
# with __GFP_RETRY_MAYFAIL — which tells the kernel to reclaim hard and then FAIL the
# allocation rather than invoke the OOM killer. So nothing is killed, nothing shows up in
# the app's metrics, and instead the box livelocks in reclaim: a full freeze down to the
# USB keyboard, needing a power cycle. The kernel trace named it exactly:
#
#   llama-server: page allocation failure: order:0, mode:...|__GFP_RETRY_MAYFAIL
#     amdgpu_ttm_tt_populate -> amdgpu_bo_create -> amdgpu_gem_create_ioctl -> drm_ioctl
#
# order:0 is a SINGLE 4 KB page. By the time that fails the box is already gone. earlyoom
# and the reclaim-headroom sysctls were ALREADY applied when it happened, so hardening
# alone is not the answer: the update has to stop competing with itself for memory.

# Host RAM in GB, for the log. /proc/meminfo is not namespaced, so this is the host's
# number even from inside the updater container. Prints "?" rather than 0 when the field
# is missing: a fabricated zero in an update log about memory would be worse than a gap.
mem_available_gb() {
  awk '/^MemAvailable:/ { printf "%d", $2 / 1048576; f = 1; exit }
       END { if (!f) printf "?" }' /proc/meminfo 2>/dev/null || echo "?"
}

# Image for the privileged one-shots below. On the PWA path this IS the image we are
# running in, so it is always present locally and never needs a pull.
HELPER_IMAGE="${JBRAIN_UPDATER_IMAGE:-docker:cli}"

# Write a host kernel knob (a path under /proc/sys), from either caller.
#
# The vm.* knobs are not namespaced — there is one set per kernel — so a privileged
# container writes exactly what the host would. That is what finally gives the
# containerized (PWA) caller a route to them: it is the path that rebuilds the gateway
# and loads a model, and until now it did that with none of the host's protection.
# Best-effort: a daemon that refuses --privileged just leaves the knob as it was.
host_kernel_write() {
  if [ -n "$HOST_UPDATE" ]; then
    echo "$2" > "/proc/sys/$1" 2>/dev/null
  else
    docker run --rm --privileged --network none "$HELPER_IMAGE" \
      sh -c "echo $2 > /proc/sys/$1" >/dev/null 2>&1
  fi
}

# Write a host file the owner cannot reach, from either caller, and optionally restart the
# unit that reads it.
#
# The containerized (PWA) branch is the point. `host_kernel_write` above already proved a
# privileged container writes the host's /proc/sys exactly as the host would; a bind mount
# extends that to a host FILE, and `nsenter` into PID 1's namespaces reaches the host's
# systemd to make a daemon re-read it. Without this, anything living in /etc is applied only
# by the host script — which the owner, who runs this box from the PWA with no terminal
# (CLAUDE.md #10), cannot invoke. That is how earlyoom's thresholds sat unapplied.
#
# Best-effort in three independent steps: the write can fail (no --privileged), the restart
# can fail (no nsenter, no systemd) without undoing the write, and a failed restart still
# leaves the file correct for the next boot. $1 = absolute host path, $2 = contents,
# $3 = optional unit to restart.
host_file_write() {
  _dir="$(dirname "$1")"
  if [ -n "$HOST_UPDATE" ]; then
    printf '%s\n' "$2" > "$1" 2>/dev/null || return 1
    [ -n "${3:-}" ] && systemctl restart "$3" >/dev/null 2>&1
    return 0
  fi
  docker run --rm --privileged --network none -v "$_dir:/hostdir" "$HELPER_IMAGE" \
    sh -c "printf '%s\n' \"\$1\" > /hostdir/\"\$2\"" -- "$2" "$(basename "$1")" \
    >/dev/null 2>&1 || return 1
  # The restart needs the HOST's systemd, which lives in PID 1's namespaces.
  [ -n "${3:-}" ] && docker run --rm --privileged --pid=host --network none "$HELPER_IMAGE" \
    nsenter -t 1 -m -u -i -n -p systemctl restart "$3" >/dev/null 2>&1
  return 0
}

# Apply a loaded module's parameter for THIS BOOT, from either caller.
#
# NOT usable for ttm.pages_limit — see the note at the call site. The parameter is
# writable (0644) and the write appears to succeed, but the GTT cap it is supposed to
# move was snapshotted at probe, so nothing changes. Kept for parameters that ARE
# re-read per use.
host_module_param_write() {
  if [ -n "$HOST_UPDATE" ]; then
    echo "$2" > "/sys/module/$1" 2>/dev/null
  else
    docker run --rm --privileged --network none "$HELPER_IMAGE" \
      sh -c "echo $2 > /sys/module/$1" >/dev/null 2>&1
  fi
}

# The earlyoom arguments, in ONE place so the host script and the containerized fallback
# cannot drift (deploy/oom-hardening.sh carries the same string; a test pins them together).
#
# 30/20 rather than the 10/5 they were: earlyoom fires only when memory AND swap are both
# under their limits, and its `-m` reads MemAvailable, which counts page cache as free. In
# the 2026-08-19 livelock swap was 100% consumed for seven hours while MemAvailable held at
# 29% — `-m 10` was never approached and this backstop never fired. Raising the memory limit
# is safe because of that AND: swap is ~100% free on a healthy box, so `-s 10` gates it.
EARLYOOM_ARGS_LINE='EARLYOOM_ARGS="-r 60 -m 30 -m 20 -s 10 -s 5 --prefer ^llama-server$ --avoid ^(sshd|systemd|systemd-.*|dockerd|containerd|postgres|supervisor)$"'

# Return the page cache to the kernel before a model load.
#
# MemAvailable counts reclaimable page cache as available — and reclaiming it under
# pressure is precisely what livelocks this box. After a build that just wrote tens of GB
# of layers, MemAvailable reads fine and cannot be realised in time, so gating on it
# without dropping first would be gating on a number we know to be optimistic. Dropping
# converts the optimism into real free pages, or proves it was never there.
drop_page_cache() {
  _before="$(mem_available_gb)"
  echo "[update] dropping page cache ($_before GB available before)"
  if [ -n "$HOST_UPDATE" ]; then
    sync
    echo 3 > /proc/sys/vm/drop_caches 2>/dev/null || true
  else
    docker run --rm --privileged --network none "$HELPER_IMAGE" \
      sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches' >/dev/null 2>&1 || true
  fi
  _after="$(mem_available_gb)"
  # Both writes are best-effort (a daemon that refuses --privileged, a read-only /proc/sys),
  # and both are silenced — so without this the log would claim a drop that never happened
  # and the gate below would be measuring exactly the reclaimable cache it must not trust.
  # The before/after numbers are the only evidence either way, so compare them out loud.
  if [ "$_after" = "$_before" ]; then
    echo "[update] WARNING: page cache did not drop (still $_after GB) — the memory check"
    echo "[update]          below may be counting cache it cannot actually reclaim"
  else
    echo "[update] page cache dropped ($_after GB available after)"
  fi
  # Explicit, because callers use this in an `&&` chain guarding the model load: a
  # future edit that ends the function on a failing command would silently skip the
  # very step this exists to protect.
  return 0
}

# Ask the gateway to release every model it holds, and say so if it wouldn't.
#
# A controlled unload, never a container kill: killing it makes the KERNEL reclaim tens of
# gigabytes of GTT-pinned pages all at once, which is the allocation storm this whole
# script exists to avoid. Bounded, because an unreachable gateway must not wedge the
# update.
release_models() {
  run_bounded "$UNLOAD_TIMEOUT_S" docker compose run --rm --no-deps -T api \
    python -m jbrain.cli local-llm-unload \
    || echo "[update] unload skipped (gateway unreachable?)"
}

# --- quiescing the stack for the memory-heavy phase --------------------------
#
# The update used to build images, recreate a dozen containers and load a model with the
# WHOLE stack resident. Nothing needs to be true at once here: the services being rebuilt
# are of no use to anyone mid-rebuild, and their memory is worth more to the build and the
# model load than their uptime is during an update nobody can use the box through.

# Every profile the compose file defines, DERIVED rather than hand-listed — the same trick
# prune-orphans.sh uses. A literal list is a second copy of the file's own profile set and
# goes stale silently (it had already missed `tools`). Compose does auto-enable the profile
# of a service named explicitly, so this is belt-and-braces; it costs one call and removes
# a class of drift.
ALL_PROFILES="$(docker compose config --profiles 2>/dev/null \
  | sed 's/^/--profile /' | tr '\n' ' ')"

# The control plane, kept up THROUGH the quiesce. Stopping these too would be simpler and
# free perhaps another gigabyte, but it would also make the PWA go dark for the whole
# update — no progress log, no way to tell a slow build from a wedged one, on the exact
# operation that has repeatedly frozen this box. That visibility is worth far more than
# ~1 GB set against the ~91 GB of models and the tens of GB of page cache this phase is
# actually fighting.
QUIESCE_KEEP="db api supervisor proxy cloudflared"

QUIESCED=""

# Where the quiesced set is recorded, so it survives the death of this process.
#
# The EXIT trap cannot cover the kills that matter: the supervisor reaps a hung one-shot
# with `remove(force=True)` (SIGKILL, untrappable) and a power cut runs nothing at all —
# and this box's whole history is hard freezes. Every service uses `restart: unless-stopped`,
# which by definition does NOT restart something explicitly stopped, so without this a
# crashed quiesce leaves the box missing its worker, embedder and speech services across
# reboots. Worse for `comfyui` and the mqtt pair: the next update's `up -d` never names
# them, so nothing would ever bring them back. The file is the durable half of the trap.
QUIESCE_STATE=".jbrain-quiesced"

# Heal a quiesce that never finished — run before this update stops anything of its own.
restore_stale_quiesce() {
  [ -f "$QUIESCE_STATE" ] || return 0
  echo "[update] a previous update left services stopped — restoring them first"
  for _svc in $(cat "$QUIESCE_STATE"); do
    docker compose $ALL_PROFILES start "$_svc" >/dev/null 2>&1 \
      || echo "[update] could not restore $_svc"
  done
  rm -f "$QUIESCE_STATE"
}

# Compose's project name (docker-compose.yml sets `name: jbrain`); the label every
# container in the stack carries, and how the supervisor scopes its own view too.
COMPOSE_PROJECT="${COMPOSE_PROJECT_NAME:-jbrain}"

quiesce_stack() {
  # Read the RUNNING set from the daemon's labels rather than `compose ps --services`.
  # That flag's meaning has differed across compose versions (v1 listed every service in
  # the FILE), and getting it wrong here is not cosmetic: unquiesce_stack would later
  # `start` services that were deliberately off — the PWA's comfyui toggle is a container
  # stop, so an update would silently switch it back on. `docker ps` without -a is
  # running-only in every version, and the label says exactly which service it is.
  for _svc in $(docker ps --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
      --format '{{.Label "com.docker.compose.service"}}' 2>/dev/null); do
    case " $QUIESCE_KEEP " in
      *" $_svc "*) continue ;;
    esac
    QUIESCED="$QUIESCED $_svc"
  done
  if [ -n "$QUIESCED" ]; then
    echo "[update] quiescing for the build and the model load:$QUIESCED"
    # One at a time, NOT one batched `stop`. Compose validates every name up front and
    # refuses the WHOLE command if any is unknown to the file — and this box carries
    # containers from renamed services (server-brain, whisper, claude-shim; see
    # prune-orphans.sh). One orphan would make the batch fail, `|| true` would swallow
    # it, and the log would still say "quiesced" over a stack that never stopped.
    # Recorded BEFORE the first stop, so a kill between the two still leaves a trail.
    printf '%s\n' $QUIESCED > "$QUIESCE_STATE" 2>/dev/null || true
    for _svc in $QUIESCED; do
      docker compose $ALL_PROFILES stop -t 30 "$_svc" >/dev/null 2>&1 \
        || echo "[update] could not stop $_svc — leaving it up"
    done
  fi
  echo "[update] quiesced ($(mem_available_gb) GB available)"
}

# Restart exactly what quiesce_stack stopped. `up -d` alone is not enough: it does not
# cover the profile-gated services (comfyui, the mqtt pair), which would otherwise stay
# down for good after an update that never mentioned them. Idempotent — `start` on a
# running service is a no-op — and clearing QUIESCED makes the EXIT trap a no-op once the
# normal path has already run.
unquiesce_stack() {
  [ -n "$QUIESCED" ] || return 0
  echo "[update] restoring quiesced services:$QUIESCED"
  for _svc in $QUIESCED; do
    # NOT `|| true`: this function is the only thing standing between a failed update and
    # a box silently missing its worker, embedder and speech services, and the owner reads
    # this log because they have no terminal. A restore that fails must SAY so.
    docker compose $ALL_PROFILES start "$_svc" >/dev/null 2>&1 \
      || echo "[update] WARNING: $_svc did not restart — start it from Ops"
  done
  QUIESCED=""
  rm -f "$QUIESCE_STATE"
}

# Stop the api for the model-load window only.
#
# The api is deliberately kept up through the build so the PWA can stream this log — but it
# runs the keep-warm prime (jbrain.llm.warm_keeper), which retries every 5s and loads the
# CHAT model the instant the gateway answers. On this box that is ~60 GB, allocated in the
# middle of an update, by a process nobody asked to do it. The smoke test itself no longer
# loads anything that big, so the prime is now the LARGEST allocation the phase could make —
# which is exactly why it still has to be stopped. Appended to QUIESCED so the ordinary
# restore path and the EXIT trap both cover it.
pause_api() {
  echo "[update] pausing api for the model load (its keep-warm prime would race it)"
  docker compose stop -t 30 api >/dev/null 2>&1 || echo "[update] could not pause api"
  QUIESCED="$QUIESCED api"
  printf '%s\n' $QUIESCED > "$QUIESCE_STATE" 2>/dev/null || true
}

# An update that dies mid-phase must not leave the box permanently half-stopped. This trap
# is the only thing between a killed updater and a box silently missing its worker,
# embedder and speech services until somebody notices. It cannot cover every death —
# the supervisor's stale-one-shot reaper uses `remove(force=True)` (SIGKILL) and a power
# cut runs nothing at all — so it is a safety net, not a guarantee.
#
# The signal handler must EXIT. A trap that merely returns lets the script resume at the
# next statement: a SIGTERM during the build would restore the whole stack and then carry
# on to load ~60 GB with everything resident — precisely the pre-fix condition, and with
# QUIESCED now empty it could never re-quiesce.
on_signal() {
  unquiesce_stack
  exit 143
}
trap unquiesce_stack EXIT
trap on_signal INT TERM

echo "[update] starting ($(mem_available_gb) GB available)"
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
# brought back deliberately, once, further down. Gated on hosting being enabled so a
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
# A previous update may have been SIGKILLed mid-quiesce (the supervisor's reaper, a power
# cut) and left services stopped. Put them back before this update takes its own census,
# or they would be invisible to it and stay down forever.
restore_stale_quiesce

# This has to run BEFORE the quiesce, not after it: quiesce_stack reads the running set
# from Docker's labels, so an orphan from a renamed service (server-brain, whisper) would
# otherwise land in the list of things to stop and, later, to start again.
# Clear containers left behind by a renamed (server-brain->wall, whisper->tts-stt) or removed
# (claude-shim) service before the `up -d` below — an old server-brain holding host port 8800
# blocks the new `wall` (which is what took the tunnel down after the split update), and any such
# labeled orphan lingers on the Ops screen. The helper sweeps by comparing labels against the
# file's full service set, so profile-gated services `--remove-orphans` would wrongly reap are kept.
echo "[update] clearing renamed/removed-service orphans"
sh src/deploy/prune-orphans.sh || echo "[update] orphan sweep skipped"

# Everything from here to the `up -d` below runs against a quiesced stack: only the
# control plane stays resident, so the build and — far more importantly — the model load
# are not competing with a dozen services for the same unified memory.
quiesce_stack

# OOM hardening, applied BEFORE the memory-heavy phase rather than after it, which is
# where it used to sit — protection installed after the thing it protects against has
# already run is decoration. Two halves, because the callers differ in capability:
#
#  - The full host script (earlyoom via apt, persistent /etc/sysctl.d) needs the real
#    host, so it stays gated on HOST_UPDATE.
#  - The reclaim-headroom knobs themselves are NOT namespaced, so the containerized
#    caller can still apply them for this boot through a privileged one-shot. That
#    matters: the PWA path is the one that rebuilds the gateway and loads a model, and
#    it was doing so with none of this in place. Values mirror oom-hardening.sh, which
#    is what makes them survive a reboot; a test pins the two together.
if grep -q '^LOCAL_LLM_ENABLED=true' .env; then
  if [ -n "$HOST_UPDATE" ] && sh src/deploy/oom-hardening.sh; then
    :
  else
    # Either the containerized caller (no apt, no /etc/sysctl.d) or a host run where the
    # full script bowed out (not root, no systemd). Apply the knobs themselves anyway —
    # they are the half that matters for the reclaim livelock, and this boot gets them
    # even when nothing can make them persist.
    echo "[update] applying reclaim headroom for this boot"
    host_kernel_write vm/min_free_kbytes 2097152 || echo "[update] min_free_kbytes not applied"
    host_kernel_write vm/watermark_scale_factor 200 || echo "[update] watermark not applied"
    host_kernel_write vm/swappiness 10 || echo "[update] swappiness not applied"
    # earlyoom's thresholds too. This is the OS backstop the residency design leans on, and
    # until now the PWA path — the only one the owner can run — left it at whatever the last
    # host install wrote. Installing the package still needs apt (the host script's job), but
    # a box that already has it gets the corrected arguments from an ordinary Update.
    if host_file_write /etc/default/earlyoom "$EARLYOOM_ARGS_LINE" earlyoom; then
      echo "[update] earlyoom thresholds applied"
    else
      echo "[update] earlyoom thresholds NOT applied — run deploy/oom-hardening.sh on the host"
    fi
    # ttm.pages_limit is deliberately NOT written here.
    #
    # This used to attempt a runtime write and report success. That was wrong, and it
    # reported a no-op as a fix from 2026-08-19 until this change. The GTT bound is not
    # the ttm_tt_populate soft loop (which, when nothing is swappable, breaks and
    # allocates anyway) — it is the resource-manager size:
    #
    #   amdgpu_ttm.c   gtt_size = ttm_tt_pages_limit() << PAGE_SHIFT   (at probe)
    #   amdgpu_gtt_mgr.c  if (ttm_resource_manager_usage(man) > man->size) return -ENOSPC;
    #
    # `man->size` is assigned once at probe and nothing re-reads the module parameter
    # afterwards. So the write succeeds, reads back the new value, and changes no cap.
    # Only the grub entry binds, which is a genuine host step with no PWA equivalent —
    # Ops -> Host settings reports the real enforced value (mem_info_gtt_total IS
    # man->size) so the owner can see whether a reboot is owed.
  fi
fi

echo "[update] building images (rev $JBRAIN_GIT_DESCRIBE)"
docker compose $JCODE_PROFILE $TUNNEL_PROFILE build

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
#
# This block runs while the stack is QUIESCED and before the recreate, not after it as it
# once did. The smoke test itself is now cheap — it loads the smallest installed model and
# runs the tool probe against that same model, rather than loading gpt-oss (~59 GB) for the
# probe, because THAT load is what the kernel traces caught freezing the host. Doing it here
# anyway is the belt to that braces: the gateway rebuild still pulls and writes multi-GB
# layers, and the keep-warm prime would still load a big model the moment the gateway
# answers (which is what pause_api below is for).
AUTO_UPDATE_ON=1
if grep -q '^LOCAL_LLM_AUTO_UPDATE=false' .env; then
  AUTO_UPDATE_ON=''
elif ! run_bounded "$TOGGLE_TIMEOUT_S" docker compose run --rm --no-deps -T api \
    python -m jbrain.cli local-llm-auto-update; then
  echo "[update] gateway auto-update is OFF (Settings) — keeping the pinned base, no model load"
  AUTO_UPDATE_ON=''
fi

# The Fast-Qwen-loads patch toggle (Settings), read through the api image like the flag
# above. It compiles a patched llama-server (Dockerfile.local-llm's PATCH_RESTORE_CHECKPOINT
# build arg, LLAMA_CPP_COMMIT from source) into the gateway image, which the qwen3.8
# MTP-hybrid disk restore depends on. It is a SECOND reason to rebuild the gateway,
# independent of tracking the newest llama.cpp: an owner who turns the patch on while
# auto-update is OFF still expects the next Update to build it, so rebuild-needed is
# auto-update OR patch. The exported var is what compose reads for the build arg
# (${LOCAL_LLM_PATCH_RESTORE_CHECKPOINT:-0}); the export is authoritative (a shell env var
# beats a `.env` line in compose), so the PWA setting — not a stray `.env` value — decides.
PATCH_ON=''
if run_bounded "$TOGGLE_TIMEOUT_S" docker compose run --rm --no-deps -T api \
    python -m jbrain.cli local-llm-patch-restore-checkpoint; then
  PATCH_ON=1
  export LOCAL_LLM_PATCH_RESTORE_CHECKPOINT=1
  echo "[update] Fast-Qwen-loads patch is ON (Settings) — building the patched llama-server"
else
  export LOCAL_LLM_PATCH_RESTORE_CHECKPOINT=0
fi

if [ -n "$LOCAL_LLM_RUNNING" ] && { [ -n "$AUTO_UPDATE_ON" ] || [ -n "$PATCH_ON" ]; }; then
  # AUTO_UPDATE_ON tracks the newest llama.cpp (FLOATING base, --pull). With auto-update OFF
  # but the patch ON we still rebuild — but on the PINNED base and with no --pull, because
  # "auto-update off" means freeze the reproducible digest; only the patch build arg changes.
  if [ -n "$AUTO_UPDATE_ON" ]; then
    FLOATING="$(sed -n 's/^LOCAL_LLM_BASE_FLOATING=//p' .env | tail -n1)"
    [ -n "$FLOATING" ] || FLOATING="docker.io/kyuz0/amd-strix-halo-toolboxes:vulkan-radv"
    echo "[update] LOCAL_LLM_AUTO_UPDATE: rebuilding gateway on newest llama.cpp ($FLOATING)"
  else
    echo "[update] rebuilding gateway on the pinned base for the Fast-Qwen-loads patch"
  fi
  # The image id BEFORE the rebuild. The smoke test exists to catch a bad UPSTREAM build,
  # so when the rebuild produces the identical image there is nothing new to vet — and the
  # test is not free: it loads a model into the iGPU, which is tens of GB of disk read and
  # minutes of the box's attention, on every routine update that changed nothing. Skipping
  # the unchanged case is what stops a no-op update from pinning the GPU. (A patch toggle
  # flips the build arg, so the image id changes and the smoke test correctly runs.)
  BEFORE_IMG="$(docker image inspect -f '{{.Id}}' jbrain2-local-llm:local 2>/dev/null || true)"
  # Everything from here until the unload can allocate tens of GB, so the api's keep-warm
  # prime must not be running alongside it.
  pause_api
  # Bounded like every other one-off: this can be a multi-GB registry pull on a rolling tag,
  # and an update that hangs here hangs with the stack quiesced. The build sub-shell branches
  # on auto-update (floating + --pull) vs patch-only (pinned base), and both inherit the
  # exported LOCAL_LLM_PATCH_RESTORE_CHECKPOINT for the build arg.
  if { if [ -n "$AUTO_UPDATE_ON" ]; then
         run_bounded "$PULL_TIMEOUT_S" env LOCAL_LLM_BASE="$FLOATING" \
           docker compose --profile local-llm build --pull local-llm
       else
         run_bounded "$PULL_TIMEOUT_S" docker compose --profile local-llm build local-llm
       fi; } \
      && docker compose --profile local-llm up -d local-llm; then
    AFTER_IMG="$(docker image inspect -f '{{.Id}}' jbrain2-local-llm:local 2>/dev/null || true)"
    if [ -n "$BEFORE_IMG" ] && [ "$BEFORE_IMG" = "$AFTER_IMG" ]; then
      echo "[update] gateway image unchanged — skipping the smoke test (nothing new to vet)"
    # Immediately before the load, and after the build that dirtied the cache — the smoke
    # test refuses a load it cannot afford (jbrain.llm.smoketest.LOAD_HEADROOM_GB), and it
    # reads MemAvailable to decide. That number counts reclaimable page cache as free, so
    # without this it would be measuring optimism rather than memory.
    elif drop_page_cache && run_bounded "$SMOKE_TIMEOUT_S" docker compose run --rm --no-deps -T api \
        python -m jbrain.cli local-llm-smoketest; then
      echo "[update] gateway smoke test passed on the newest llama.cpp"
    else
      SMOKE_FAILED=1
    fi
  else
    SMOKE_FAILED=1
  fi
  if [ -n "${SMOKE_FAILED:-}" ]; then
    SMOKE_VERDICT="the gateway did not pass its smoke test — it was rolled back to its pinned base"
    echo "[update] WARNING: $SMOKE_VERDICT"
    # A failed PATCHED build turns its OWN toggle back off: the patch is what dirtied the
    # image (the patched llama-server could not load a model), so leaving the setting on would
    # rebuild the same broken engine on every future Update and keep KvPrefixStore writing
    # inert restores. Clearing it here — and dropping the build arg for the rollback rebuild
    # below — puts the box back on the stock binary, off. The API GET then reflects OFF, so the
    # PWA toggle shows what actually happened. Best-effort: a DB hiccup here must not abort the
    # rollback (set -e is on), so the setter's failure is tolerated.
    if [ -n "$PATCH_ON" ]; then
      export LOCAL_LLM_PATCH_RESTORE_CHECKPOINT=0
      run_bounded "$TOGGLE_TIMEOUT_S" docker compose run --rm --no-deps -T api \
        python -m jbrain.cli set-local-llm-patch-restore-checkpoint off \
        || echo "[update] WARNING: could not clear the Fast-Qwen-loads patch setting"
    fi
    # EMPTY IT FIRST. A smoke test fails at the tool probe with BOTH models loaded, and a
    # timeout leaves a load still running — so at this point the gateway can be holding
    # ~90 GB. `up -d` on a changed image force-recreates it, and with no stop_grace_period
    # that is SIGKILL in 10s: the kernel then reclaims all of it at once, while the rebuild
    # below is allocating. That is verbatim the collision this script forbids everywhere
    # else, and it sat on the one branch nobody exercises.
    release_models
    # No LOCAL_LLM_BASE override and no --pull: rebuild against the reproducible pinned
    # digest (compose default or the operator's .env value) from cached layers. The patch
    # build arg was cleared above when a patched build was the thing that failed.
    docker compose --profile local-llm build local-llm \
      && docker compose --profile local-llm up -d local-llm \
      || echo "[update] WARNING: gateway rollback rebuild failed — check 'jbrain logs local-llm'"
  fi
fi

# Release whatever is loaded before the stack comes back. The smoke test's own model is
# small now, but this also catches anything the gateway picked up on its own, and the rule
# stands either way: recreating a dozen containers on top of pinned unified memory is the
# same collision the pre-build unload exists to prevent, arriving from the other end. The
# gateway keeps running, holding nothing — it reloads on demand.
# Gated on the same rebuild-needed condition (auto-update OR patch): only those branches
# start the gateway above (it is still removed otherwise), so without a start this would
# spend a compose run on an unreachable target and log a reassuring line about memory it
# never freed.
if [ -n "$LOCAL_LLM_RUNNING" ] && { [ -n "$AUTO_UPDATE_ON" ] || [ -n "$PATCH_ON" ]; }; then
  release_models
  echo "[update] gateway emptied before the recreate ($(mem_available_gb) GB available)"
fi

# Migrations run as late as possible — immediately before the recreate that brings up the
# code they are for. They used to sit above the gateway work, which meant the still-running
# OLD-image api served the owner's PWA against a migrated schema for the whole gateway
# rebuild and smoke test. That was defensible when the window was seconds; it is not now
# that the window includes an unconditional multi-GB base-image pull on a rolling tag.
#
# What kept them up there was the Settings read below running on the new image: it would
# now see a pre-migration schema. That read is explicitly schema-tolerant — an unreachable
# or unreadable DB returns "auto-update ON" by design (jbrain.cli._print_auto_update),
# because failing closed would freeze the gateway on an old base on any DB hiccup. So it
# degrades to the default, and the live api is the larger half of the trade.
echo "[update] running migrations"
docker compose run --rm migrate

echo "[update] restarting stack"
docker compose $JCODE_PROFILE $TUNNEL_PROFILE up -d

# End of the quiesced window. `up -d` covers the base stack; this puts back the
# profile-gated services it does not name (comfyui, the mqtt pair) so an update never
# silently leaves a feature the operator enabled switched off.
unquiesce_stack

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

# Bring the LLM gateway back up now the churn is over (it loads models on demand).
# Only when it was up before this update: the model sync above restarts it when the
# roster CHANGED, but the common no-op sync ("no models to sync") exits before its
# own `up -d`, so without this an enabled gateway would stay stopped after every
# routine update. Idempotent — with the sync's own start, and with the auto-update
# block's, which already brought it back inside the quiesced window; best-effort. This
# is the ONLY thing that restarts it when auto-update is off and nothing rebuilt it.
if [ -n "$LOCAL_LLM_RUNNING" ]; then
  echo "[update] restarting local-llm gateway"
  docker compose --profile local-llm up -d local-llm || true
fi


# Reclaim space, but never let a prune hiccup fail the whole update (set -e) after
# the real work is done — a transient daemon error here once surfaced as a bogus
# "update failed" with a fully-updated stack.
docker image prune -f || true
docker builder prune -f --keep-storage 10GB >/dev/null 2>&1 || true
echo "[update] complete"
