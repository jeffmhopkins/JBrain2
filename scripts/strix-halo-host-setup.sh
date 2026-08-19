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
# amd_iommu=off: a benchmark tweak, contested upstream — kept only because it is already
# deployed here. `amdgpu.gttsize` is DEPRECATED (the kernel prints a warning pointing at
# ttm.pages_limit) and is no longer written.
#
# `ttm.pages_limit` is the one that has to be RIGHT, not just large, and the mechanism is
# worth naming precisely because two comments in this repo used to get it wrong.
#
# It is NOT the `ttm_tt_populate` soft loop. That loop compares ttm_pages_allocated to the
# limit and calls ttm_global_swapout(), but a return of 0 (nothing swappable — i.e. every
# BO pinned, which is exactly a serving model) BREAKS the loop and allocates anyway; and
# -ENOSPC cannot even escape that path, since ttm_bo_swapout_cb rewrites it to -EBUSY and
# ttm_lru_walk_for_evict rewrites -EBUSY to 0.
#
# The real bound is the GTT RESOURCE MANAGER size:
#
#   amdgpu_ttm.c      gtt_size = ttm_tt_pages_limit() << PAGE_SHIFT      (at probe)
#   amdgpu_gtt_mgr.c  if (ttm_resource_manager_usage(man) > man->size) return -ENOSPC;
#
# That is a genuine allocator refusal before pages are handed out. Two consequences: the
# value only binds if it is BELOW MemTotal (the stock default is already MemTotal/2, so a
# larger value RAISES the ceiling rather than setting one), and it is read exactly once at
# probe — so this must be set via grub. A runtime write to /sys/module/ttm/parameters/
# pages_limit succeeds, reads back the new value, and moves no cap. `mem_info_gtt_total`
# reports man->size and is therefore the only honest readout of what is enforced.
#
# Without a binding value the box does not OOM-kill and recover — every task enters direct
# reclaim, finds nothing reclaimable, and the machine simply stops answering. That is the
# freeze this host has taken repeatedly, most recently a seven-hour livelock on
# 2026-08-19 that needed a power cycle.
#
# This used to hardcode 32505856 pages = 124 GiB ≈ 100% of RAM on this box, which DISABLES
# the backstop it exists to provide (kernel 6.18 has no physical-RAM sanity cap of its own;
# that lands in v7.2). It is now derived: MemTotal minus a 16 GiB host reserve, so the
# kernel refuses the allocation while there is still enough RAM left to stay responsive.
RESERVE_GIB=16
PAGES_LIMIT="$(awk -v reserve="$RESERVE_GIB" '
  /^MemTotal:/ { kb = $2 }
  END { pages = (kb - reserve * 1048576) / 4; if (pages < 1048576) pages = 1048576;
        printf "%d", pages }
' /proc/meminfo)"
say "ttm.pages_limit=$PAGES_LIMIT pages ($((PAGES_LIMIT / 262144)) GiB) — MemTotal less ${RESERVE_GIB} GiB"
GRUB_FILE=/etc/default/grub
PARAMS="amd_iommu=off ttm.pages_limit=$PAGES_LIMIT"
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
# Key-absence-only merging was a silent no-op on the box that needed this most: it
# already carried ttm.pages_limit=32505856 (124 GiB, above MemTotal, so binding
# nothing), and a re-run printed "already present" and changed it. Params whose VALUE
# is load-bearing are now replaced, not skipped.
REPLACE = {"ttm.pages_limit"}
# Deprecated by the kernel in favour of ttm.pages_limit; removed if present.
DROP = {"amdgpu.gttsize"}

want = {p.split("=", 1)[0]: p for p in params}
kept, changed = [], []
for tok in cur.split():
    k = tok.split("=", 1)[0]
    if k in DROP:
        changed.append(f"-{tok}")
        continue
    if k in REPLACE and k in want and want[k] != tok:
        changed.append(f"{tok} -> {want[k]}")
        kept.append(want.pop(k))
        continue
    want.pop(k, None)          # already present and acceptable
    kept.append(tok)
add = list(want.values())
changed.extend(add)
if changed:
    line = f'{key}="{" ".join(kept + add).strip()}"'
    src = re.sub(rf'^{key}=".*"$', line, src, count=1, flags=re.M) if m else src + f"\n{line}\n"
open(os.environ["GRUB_TMP"], "w").write(src)
print(" ".join(changed))
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
