#!/usr/bin/env bash
# Copy the built panel firmware into `firmware/dist/`, which is what actually ships.
#
# The box flashes `firmware/dist/` off its own checkout — it does not download a release
# (backend/src/jbrain/api/endpoint.py explains why) — so these committed bytes ARE the
# firmware. Bumping `firmware/version.txt` without running this ships the old image under
# a new version number; `.github/workflows/firmware.yml` fails the PR when that happens.
#
# BUMP `firmware/version.txt` FIRST, THEN BUILD. The version is compiled INTO the image (IDF
# reads that file into the app descriptor), so bumping it afterwards ships the old number in
# the bytes and fails CI. This script refuses to copy an image whose embedded version does not
# match the file.
#
#   scripts/firmware-setup.sh            # once — installs ESP-IDF v5.5.5
#   echo 0.2.37 > firmware/version.txt
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
         "$build/partition_table/partition-table.bin" "$build/ota_data_initial.bin" \
         "$build/srmodels/srmodels.bin"; do
  if [ ! -f "$f" ]; then
    echo "missing $f — build the firmware first (see the header of this script)" >&2
    exit 1
  fi
done

# THE IMAGE CARRIES THE VERSION, AND THAT IS THE TRAP THIS CHECK EXISTS FOR.
# ESP-IDF reads `firmware/version.txt` at BUILD time into the app descriptor at 0x30, so
# bumping the file after building stamps the previous number into the bytes that ship. The
# result is a dist/ that differs from CI's build in exactly 66 bytes, all of them the version
# string and the ELF hash that covers it — which is a confusing way to be told "you built
# before you bumped". It has now happened twice (0.2.35 and 0.2.36), both times costing a CI
# round, so it is a check rather than a thing to remember.
want="$(cat "$root/firmware/version.txt")"
got="$(python3 - "$build/jbrain-endpoint.bin" <<'EOF'
import sys
with open(sys.argv[1], 'rb') as f:
    f.seek(0x30)  # 0x18 image header + 0x8 segment header + 0x10 into esp_app_desc_t
    print(f.read(32).split(b'\0')[0].decode())
EOF
)"
if [ "$want" != "$got" ]; then
  echo "firmware/version.txt says $want but the built image says $got." >&2
  echo "Bump version.txt FIRST, then rebuild: (cd firmware && idf.py build)" >&2
  exit 1
fi

mkdir -p "$dist"
cp "$build/jbrain-endpoint.bin" "$build/bootloader/bootloader.bin" \
   "$build/partition_table/partition-table.bin" "$build/ota_data_initial.bin" \
   "$build/srmodels/srmodels.bin" "$dist/"

# Byte-for-byte the form the workflow regenerates and diffs against, so a mismatch there
# is always a real image difference rather than a formatting one.
( cd "$dist" && sha256sum ./*.bin | sed 's| \./| |' > SHA256SUMS )

echo "firmware/dist/ now holds $(cat "$root/firmware/version.txt"):"
cat "$dist/SHA256SUMS"
