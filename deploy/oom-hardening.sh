#!/usr/bin/env bash
# Host OOM hardening for a unified-memory box (AMD Strix Halo). See
# docs/runbooks/STRIX_HALO_SETUP.md, "Stability — hard-freeze / OOM hardening".
#
# On this hardware the kernel does NOT reliably OOM-kill a sudden overcommit — it can
# livelock in page reclaim and hard-freeze (a full power-cycle). The app's residency
# budget is the primary guard, but it is a PER-PROCESS evictor: the api and the worker
# each hold the free-RAM floor on their own, so a deferred worker model-load overlapping
# an api one can still momentarily co-load past the floor. Back that with an OS net that
# kills ONE llama-server (a reloadable model) before the kernel stalls, and start reclaim
# early enough that the killer actually gets CPU to act.
#
# Called by scripts/local-llm-setup.sh (install / enable-local-models) AND by the host
# `jbrain update` path (deploy/jbrain), so an existing box gets hardened on its next
# update without a manual step. Idempotent and best-effort by design: every step degrades
# to a warning, so a hardening hiccup never blocks setup or an update. Must run as root
# (writes /etc/default, /etc/sysctl.d, drives systemctl) — a non-root or non-systemd host
# is skipped with a notice rather than failing.
set -u

say() { printf '\n[oom-hardening] %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { say "not root — skipping OOM hardening"; exit 0; }
command -v systemctl >/dev/null 2>&1 || { say "no systemd — skipping OOM hardening"; exit 0; }

if ! command -v earlyoom >/dev/null 2>&1; then
  say "Installing earlyoom (OOM backstop)"
  apt-get update -qq && apt-get install -y -qq earlyoom \
    || { say "WARNING: earlyoom install failed — box is unhardened against overcommit"; exit 0; }
fi

# -r 60: report hourly-ish. -m 10 then -m 5: SIGTERM at 10% RAM free, SIGKILL at 5% (same
# for swap via -s). --prefer a llama-server (evicting a model is recoverable); --avoid the
# processes whose death takes the box or the data down.
printf 'EARLYOOM_ARGS="%s"\n' \
  '-r 60 -m 10 -m 5 -s 5 -s 3 --prefer ^llama-server$ --avoid ^(sshd|systemd|systemd-.*|dockerd|containerd|postgres|supervisor)$' \
  > /etc/default/earlyoom
systemctl enable --now earlyoom >/dev/null 2>&1 || true
systemctl restart earlyoom >/dev/null 2>&1 || say "WARNING: could not (re)start earlyoom"

# Start reclaiming earlier and thrash into swap less, so the killer gets CPU to act BEFORE
# a unified-memory reclaim livelock — the failure mode earlyoom alone can lose to.
cat > /etc/sysctl.d/99-jbrain-oom.conf <<'SYSCTL'
vm.min_free_kbytes = 2097152
vm.watermark_scale_factor = 200
vm.swappiness = 10
SYSCTL
sysctl --system >/dev/null 2>&1 || say "WARNING: could not apply sysctl OOM tuning"
say "OOM hardening in place (earlyoom + reclaim headroom)"
