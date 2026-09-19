#!/usr/bin/env bash
# Installs the ESP-IDF toolchain for the room endpoint firmware (firmware/).
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

# esp32s3 only: the other targets' toolchains are another gigabyte for hardware we do not own.
say "installing the xtensa-esp32s3 toolchain"
"$IDF_DIR/install.sh" esp32s3

say "done — now: . $IDF_DIR/export.sh && (cd firmware && idf.py build)"
