#!/usr/bin/env bash
# Rebuilds the two photoreal shots on the README, ../hero-plate.jpg and ../hero-stand.jpg:
# the recommended back plate and the recommended desk stand, rendered in Blender (Cycles) with
# the display from Waveshare's STEP and the firmware's own pet on the glass.
#
# Heavy and optional: make_renders.sh does not call it, and a model change only needs it if the
# change shows from outside. Needs python3.11 (bpy 4.2 is built for it), OpenSCAD 2021+, a C
# compiler, and the network once: two venvs and Waveshare's 3D model go into $CACHE.
# Usage: ./make_photoreal.sh   (from this folder; WIDTH and SAMPLES override 1600 and 256)
set -euo pipefail
cd "$(dirname "$0")"
MODEL=../../amoled18_back_plate.scad
PRESETS=../../amoled18_back_plate.json
FIRMWARE=../../../../firmware/main
CACHE=${PHOTOREAL_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/amoled18-photoreal}
WIDTH=${WIDTH:-1600}
SAMPLES=${SAMPLES:-256}
STEP_URL=https://files.waveshare.com/wiki/ESP32-S3-Touch-AMOLED-1.8/ESP32-S3-Touch-AMOLED-1.8-3D.zip
STEP_SHA=d0b65e213ab8de39b4d0e04684d2b8a06b389c501685b091c8d42252f2cfd4e4
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$CACHE" "$WORK/plate" "$WORK/stand"

# Two venvs: bpy and OCP each pin their own native libraries.
if [ ! -x "$CACHE/blender/bin/python" ]; then
  python3.11 -m venv "$CACHE/blender"
  "$CACHE/blender/bin/pip" install -q "bpy==4.2.23"
fi
if [ ! -x "$CACHE/tools/bin/python" ]; then
  python3.11 -m venv "$CACHE/tools"
  "$CACHE/tools/bin/pip" install -q "cadquery-ocp==8.0.1.0.0" "trimesh==5.1.0" "pillow==12.3.0"
fi

echo "Display module (Waveshare's STEP)"
if [ ! -f "$CACHE/module.glb" ]; then
  curl -fsSL -o "$CACHE/3d.zip" "$STEP_URL"
  echo "$STEP_SHA  $CACHE/3d.zip" | sha256sum -c --quiet
  unzip -o -q "$CACHE/3d.zip" -d "$CACHE"
  "$CACHE/tools/bin/python" step2glb.py "$CACHE/ESP32-S3-Touch-AMOLED-1.8-3D.stp" "$CACHE/module.glb"
fi
cp "$CACHE/module.glb" "$WORK/"

echo "Parts (OpenSCAD)"
export_part() {  # <shot> <preset> <which>
  openscad -o "$WORK/$1/$3.stl" -p "$PRESETS" -P "$2" -D "which=\"$3\"" parts.scad 2>&1 \
    | grep -iE "warning|error" || true
}
PLATE="103035 1000mAh on edge (default)"
STAND="Desk stand: box for 103035 on edge"
for w in plate marker_plate shell buttons; do export_part plate "$PLATE" $w & done
for w in plate box screws marker_box marker_head marker_seam shell buttons; do
  export_part stand "$STAND" $w &
done
wait
"$CACHE/tools/bin/python" marker.py "$WORK/plate/poses.json" "$WORK/plate/marker_plate.stl"
"$CACHE/tools/bin/python" marker.py "$WORK/stand/poses.json" \
  "$WORK/stand/marker_box.stl" "$WORK/stand/marker_head.stl" "$WORK/stand/marker_seam.stl"

echo "Screen (the firmware's face.c)"
cc -O2 -I"$FIRMWARE" -o "$WORK/pet_frame" pet_frame.c \
  "$FIRMWARE"/{face,emotion,rig,variants,font}.c -lm
"$WORK/pet_frame" 1 "$WORK/pet_up.ppm" 0          # colour 1, the ostrich
"$WORK/pet_frame" 1 "$WORK/pet_side.ppm" 0 side
"$CACHE/tools/bin/python" screens.py "$WORK"

for shot in plate stand; do
  echo "Rendering $shot ($WIDTH px, $SAMPLES samples)"
  "$CACHE/blender/bin/python" hero.py $shot "$WORK" "$WORK/$shot.png" "$WIDTH" "$SAMPLES" 2>&1 \
    | grep -iE "error|traceback" || true
  "$CACHE/tools/bin/python" -c "import sys; from PIL import Image; \
Image.open(sys.argv[1]).convert('RGB').save(sys.argv[2], quality=90, optimize=True)" \
    "$WORK/$shot.png" "../hero-$shot.jpg"
  echo "  ../hero-$shot.jpg"
done
