#!/usr/bin/env bash
# Installs the ESP-IDF toolchain for the panel firmware (firmware/, and firmware-c6/ with
# IDF_TARGETS=esp32c6).
#
# Opt-in and separate from scripts/dev-setup.sh on purpose: this is ~3.5 GB and ~10 minutes
# for a package most sessions never touch, and CI (.github/workflows/firmware.yml) is the
# authority on whether the firmware builds. Run it when you want to compile locally.
#
# Idempotent — an existing checkout at the pinned tag is reused rather than re-cloned.
set -euo pipefail

# Pinned to the version Waveshare's examples for the ESP32-S3-Touch-AMOLED-1.8 target, so the
# vendor drivers later waves port from compile against the same headers. Keep in step with
# .github/workflows/firmware.yml.
IDF_VERSION="${IDF_VERSION:-v5.5.5}"
IDF_DIR="${IDF_PATH:-$HOME/esp-idf}"

say() { printf '\n[firmware-setup] %s\n' "$*"; }

if [ -d "$IDF_DIR/.git" ] && git -C "$IDF_DIR" describe --tags --exact-match >/dev/null 2>&1 \
  && [ "$(git -C "$IDF_DIR" describe --tags --exact-match)" = "$IDF_VERSION" ]; then
  say "esp-idf $IDF_VERSION already at $IDF_DIR"
else
  say "cloning esp-idf $IDF_VERSION into $IDF_DIR (~2.5 GB)"
  rm -rf "$IDF_DIR"
  git clone -b "$IDF_VERSION" --depth 1 --recursive \
    https://github.com/espressif/esp-idf.git "$IDF_DIR"
fi

# Only the targets asked for: each toolchain is another gigabyte. The default is the twins'
# S3 panels (firmware/); IDF_TARGETS=esp32c6 adds the RISC-V toolchain for Jeff's C6 jpanel
# (firmware-c6/), and IDF_TARGETS=esp32s3,esp32c6 installs both into the same checkout.
IDF_TARGETS="${IDF_TARGETS:-esp32s3}"
say "installing the toolchain(s) for $IDF_TARGETS"
"$IDF_DIR/install.sh" "$IDF_TARGETS"

say "done — now: . $IDF_DIR/export.sh && (cd firmware && idf.py build)"
say "        or: . $IDF_DIR/export.sh && (cd firmware-c6 && idf.py build)"
