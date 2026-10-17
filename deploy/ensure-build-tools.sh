#!/bin/sh
# Make sure every tool a llama.cpp source build invokes is on PATH, inside the builder
# stage of Dockerfile.flash-next and Dockerfile.local-llm (whose base is kyuz0's Fedora
# toolbox). Exits non-zero, before any compile starts, when one is still missing.
#
# The base is a rolling image that has shipped with /usr/bin/ld gone while its RPM DB
# still claims binutils, so a plain install says "Nothing to do". A repair pass then has
# to put the files back, and it runs ONE PACKAGE AT A TIME: dnf resolves a reinstall as a
# single transaction, and a package whose installed build has aged out of the mirrors
# fails the whole of it. That is how the 2026-10-17 Flash-Next build died — git 2.55.0
# was no longer in the Fedora 43 repos, so `dnf reinstall git cmake gcc-c++ binutils make`
# refused outright and binutils, the one package that needed it, was never touched.
set -eu

TOOLS="git cmake c++ ld make"
# Where a recovered `ld` link goes; overridable so the tests never touch the real one.
LINK_DIR="${BUILD_TOOLS_LINK_DIR:-/usr/local/bin}"

tools_missing() {
  m=""
  for t in $TOOLS; do
    command -v "$t" >/dev/null 2>&1 || m="$m $t"
  done
  printf '%s' "$m"
}

rpm_pkg() {
  case "$1" in
    c++) echo gcc-c++ ;;
    ld) echo binutils ;;
    *) echo "$1" ;;
  esac
}

# Reinstall where the installed build is still in the repos; distro-sync where it is not,
# which moves it to the build the repos do carry and lays its files down either way.
rpm_repair() {
  mgr="$1"
  for t in $(tools_missing); do
    p="$(rpm_pkg "$t")"
    echo "[build-tools] repairing $p (provides $t)"
    "$mgr" -y reinstall "$p" || "$mgr" -y distro-sync "$p" || true
  done
}

# Fedora's binutils points /usr/bin/ld at ld.bfd through alternatives; a base that lost
# the link but kept the real linker builds fine once something named `ld` is on PATH.
link_ld() {
  command -v ld >/dev/null 2>&1 && return 0
  bfd="$(command -v ld.bfd 2>/dev/null || true)"
  [ -n "$bfd" ] || return 0
  echo "[build-tools] ld missing but $bfd present — linking it as $LINK_DIR/ld"
  mkdir -p "$LINK_DIR"
  ln -sf "$bfd" "$LINK_DIR/ld"
}

if [ -z "$(tools_missing)" ]; then
  echo "[build-tools] all present: $TOOLS"
  exit 0
fi

echo "[build-tools] installing (missing:$(tools_missing))"
if command -v dnf >/dev/null 2>&1; then
  dnf -y --setopt=install_weak_deps=False install git cmake gcc-c++ binutils make
  rpm_repair dnf
elif command -v microdnf >/dev/null 2>&1; then
  microdnf -y --setopt=install_weak_deps=0 install git cmake gcc-c++ binutils make
  rpm_repair microdnf
elif command -v apt-get >/dev/null 2>&1; then
  apt-get update && apt-get install -y --no-install-recommends git cmake build-essential
  [ -z "$(tools_missing)" ] \
    || apt-get install -y --reinstall --no-install-recommends git cmake build-essential binutils \
    || true
else
  echo "[build-tools] FATAL: missing tools ($(tools_missing)) and no known package manager (dnf/microdnf/apt-get)" >&2
  exit 1
fi
link_ld

if [ -n "$(tools_missing)" ]; then
  echo "[build-tools] FATAL: build tools still missing after install/repair:$(tools_missing)" >&2
  exit 1
fi
echo "[build-tools] all present: $TOOLS"
