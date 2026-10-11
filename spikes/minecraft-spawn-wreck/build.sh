#!/usr/bin/env bash
# Generate both builds and render previews. Needs python3 + numpy + Pillow, and Blender
# (apt install blender) for the renders. Usage: ./build.sh [view,view,...]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cache="${WRECK_CACHE:-$HOME/.cache/jbrain-wreck}"
out="${WRECK_OUT:-$cache/out}"
mkdir -p "$out"
[ -d "$cache/tex" ] || "$here/fetch_textures.sh"
python3 "$here/gen_pod.py" "$out/pod.json"
python3 "$here/gen_battleship.py" "$out/pod.json" "$out/scene.json"
views="${1:-from_ground,ship_quarter,in_bridge}"
FAR_GROUND=63.9 SKY=0.4 EXPOSURE="${EXPOSURE:--1.0}" SAMPLES="${SAMPLES:-16}" RESX="${RESX:-960}" \
  blender -b -P "$here/render.py" -- "$out/scene.json" "$cache/tex" "$out/renders" "" "$views"
echo "renders in $out/renders"
