#!/usr/bin/env bash
# Copy the built panel firmware into `firmware/dist/`, which is what actually ships.
#
# The box flashes `firmware/dist/` off its own checkout — it does not download a release
# (backend/src/jbrain/api/endpoint.py explains why) — so these committed bytes ARE the
# firmware. Bumping `firmware/version.txt` without running this ships the old image under
# a new version number; `.github/workflows/firmware.yml` fails the PR when that happens.
#
# Build first:
#   scripts/firmware-setup.sh            # once — installs ESP-IDF v5.5.5
#   . ~/esp-idf/export.sh && (cd firmware && idf.py build)
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
build="$root/firmware/build"
dist="$root/firmware/dist"

# `srmodels.bin` is the odd one out and is here on purpose. It is not built FROM this source
# — it is what esp-sr packs for the wake word and command model selected in
# `sdkconfig.defaults` — and it is the only image OTA can never deliver, because
# `esp_https_ota` writes app slots and the models live in a data partition. So it ships in
# `dist/` and reaches a panel through the one USB flash each unit gets.
for f in "$build/jbrain-endpoint.bin" "$build/bootloader/bootloader.bin" \
         "$build/partition_table/partition-table.bin" "$build/srmodels/srmodels.bin"; do
  if [ ! -f "$f" ]; then
    echo "missing $f — build the firmware first (see the header of this script)" >&2
    exit 1
  fi
done

mkdir -p "$dist"
cp "$build/jbrain-endpoint.bin" "$build/bootloader/bootloader.bin" \
   "$build/partition_table/partition-table.bin" "$build/srmodels/srmodels.bin" "$dist/"

# Byte-for-byte the form the workflow regenerates and diffs against, so a mismatch there
# is always a real image difference rather than a formatting one.
( cd "$dist" && sha256sum ./*.bin | sed 's| \./| |' > SHA256SUMS )

echo "firmware/dist/ now holds $(cat "$root/firmware/version.txt"):"
cat "$dist/SHA256SUMS"
