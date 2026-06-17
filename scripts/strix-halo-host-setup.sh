#!/usr/bin/env bash
# OPTIONAL host prep for AMD Strix Halo (Ryzen AI Max+ 395, gfx1151) on Ubuntu.
#
#   sudo bash scripts/strix-halo-host-setup.sh            # interactive
#   sudo bash scripts/strix-halo-host-setup.sh --yes      # no prompts
#   sudo bash scripts/strix-halo-host-setup.sh --no-tuned # skip the tuned profile
#
# One-time host configuration the gateway image can't supply, from
# strix-halo-toolboxes.com/#config. It is NEVER run by the installer or
# dev-setup — it edits GRUB and udev and needs a reboot, so it's a deliberate,
# separate step. Idempotent: re-running only adds what's missing.
#
# It does NOT touch the JBrain stack — run it once on the host, reboot, then
# `jbrain enable-local-models`.
set -euo pipefail

ASSUME_YES=0
WITH_TUNED=1
for arg in "$@"; do
  case "$arg" in
    --yes|-y) ASSUME_YES=1 ;;
    --no-tuned) WITH_TUNED=0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n[strix-halo] %s\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq python3; }

confirm() { # confirm <prompt>; honored unless --yes
  [ "$ASSUME_YES" -eq 1 ] && return 0
  read -rp "$1 [y/N]: " a && [ "$a" = y -o "$a" = Y ]
}

# Best-effort hardware sanity — warn, don't block.
if ! grep -qi 'AMD' /proc/cpuinfo 2>/dev/null; then
  say "WARNING: this doesn't look like an AMD host — these tweaks target Strix Halo."
fi

# --- 1. Kernel boot parameters (unified-memory sizing + GPU perf) ------------
# amd_iommu=off: better GPU access on unified memory; gttsize/pages_limit let
# the iGPU address ~124GB of the shared pool. Added only if absent (an existing
# value is respected, not overwritten).
GRUB_FILE=/etc/default/grub
PARAMS="amd_iommu=off amdgpu.gttsize=126976 ttm.pages_limit=32505856"
if [ -f "$GRUB_FILE" ]; then
  # Compute the merged file into a temp WITHOUT touching the original; print the
  # params that would be added. We only commit it after confirmation.
  GRUB_TMP="$(mktemp)"
  ADDED="$(GRUB_FILE="$GRUB_FILE" GRUB_TMP="$GRUB_TMP" PARAMS="$PARAMS" python3 - <<'PY'
import os, re
src = open(os.environ["GRUB_FILE"]).read()
params = os.environ["PARAMS"].split()
key = "GRUB_CMDLINE_LINUX_DEFAULT"
m = re.search(rf'^{key}="(.*)"$', src, re.M)
cur = m.group(1) if m else ""
have = {t.split("=", 1)[0] for t in cur.split()}
add = [p for p in params if p.split("=", 1)[0] not in have]
if add:
    line = f'{key}="{(cur + " " + " ".join(add)).strip()}"'
    src = re.sub(rf'^{key}=".*"$', line, src, count=1, flags=re.M) if m else src + f"\n{line}\n"
open(os.environ["GRUB_TMP"], "w").write(src)
print(" ".join(add))
PY
)"
  if [ -n "$ADDED" ]; then
    say "GRUB: would add kernel params:$ADDED"
    if confirm "Apply this GRUB change (backs up $GRUB_FILE; needs a reboot)?"; then
      cp -a "$GRUB_FILE" "$GRUB_FILE.bak.$(date +%s)"
      cat "$GRUB_TMP" > "$GRUB_FILE"
      if command -v update-grub >/dev/null 2>&1; then update-grub
      else grub-mkconfig -o /boot/grub/grub.cfg; fi
      REBOOT_NEEDED=1
    else
      say "Skipped GRUB change (original untouched)."
    fi
  else
    say "GRUB: kernel params already present — nothing to do."
  fi
  rm -f "$GRUB_TMP"
else
  say "WARNING: $GRUB_FILE not found — skipping kernel params (non-GRUB boot?)."
fi

# --- 2. GPU device permissions ----------------------------------------------
# Group membership is what the dockerized gateway relies on (it joins the
# numeric host GIDs). The 0666 udev rule is the toolbox/distrobox convenience
# from the upstream guide; it is world-rw on the render/kfd nodes — fine on a
# single-user box, but skip it if that's too permissive for you.
TARGET_USER="${SUDO_USER:-$USER}"
say "Adding $TARGET_USER to the video,render groups."
usermod -aG video,render "$TARGET_USER" || say "WARNING: usermod failed for $TARGET_USER."

if confirm "Install the permissive 0666 udev rule for /dev/kfd + renderD* (upstream guide)?"; then
  printf '%s\n%s\n' \
    'SUBSYSTEM=="kfd", KERNEL=="kfd", MODE="0666"' \
    'SUBSYSTEM=="drm", KERNEL=="renderD*", MODE="0666"' \
    > /etc/udev/rules.d/70-kfd.rules
  udevadm control --reload-rules && udevadm trigger
  say "udev rule installed."
fi

# --- 3. Performance profile (optional) --------------------------------------
if [ "$WITH_TUNED" -eq 1 ]; then
  say "Installing tuned + accelerator-performance profile."
  apt-get update -qq && apt-get install -y -qq tuned
  systemctl enable --now tuned
  tuned-adm profile accelerator-performance || say "WARNING: could not set tuned profile."
fi

# --- 4. Report ---------------------------------------------------------------
say "GPU device nodes: $(ls /dev/dri 2>/dev/null | tr '\n' ' ')$( [ -e /dev/kfd ] && echo '/dev/kfd' )"
command -v vulkaninfo >/dev/null 2>&1 && vulkaninfo --summary 2>/dev/null | grep -i 'deviceName' | head -1 || true

say "Done. Group changes take effect on next login; kernel params need a REBOOT."
[ "${REBOOT_NEEDED:-0}" -eq 1 ] && say "Reboot, then run: jbrain enable-local-models"
