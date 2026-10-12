#!/usr/bin/env bash
# Builds the sim and renders every mock board's state to <out>/<state>.png, plus a debug
# snapshot of a short session (<out>/session.json). Then the checks CI relies on: every app
# opens and closes back to the same widget and timer count, a scripted finger meets the shell's
# touch rules, and the snapshot carries no provisioning secret.
#
#   firmware-c6/sim/render.sh [out-dir] [build-dir]
#
# LVGL is fetched at the pinned release on first configure; set
# FETCHCONTENT_SOURCE_DIR_LVGL to an existing checkout to skip the download.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-$here/out}"
build="${2:-$here/build}"
mkdir -p "$out"

cmake_args=(-S "$here" -B "$build")
if [ -n "${FETCHCONTENT_SOURCE_DIR_LVGL:-}" ]; then
  cmake_args+=("-DFETCHCONTENT_SOURCE_DIR_LVGL=$FETCHCONTENT_SOURCE_DIR_LVGL")
fi
cmake "${cmake_args[@]}" >/dev/null
cmake --build "$build" -j"$(nproc)" >/dev/null

sim="$build/jpanel-sim"
for state in $("$sim" --list); do
  "$sim" --state "$state" --png "$out/$state.png"
done
"$sim" --state session --snapshot "$out/session.json" >/dev/null

"$sim" --lifecycle
"$sim" --touch

# The fake provisioning holds a password, a device key and a CA shaped like the real ones
# (svc_settings_fake.h). None may reach a snapshot, whatever a describer or a log line did.
for secret in fake-wifi-passphrase dk_fake_device_key fakeboxroot; do
  if grep -q "$secret" "$out/session.json"; then
    echo "SECRET LEAKED into the debug snapshot: $secret" >&2
    exit 1
  fi
done
echo "rendered $(ls "$out"/*.png | wc -l) states into $out; snapshot clean"
