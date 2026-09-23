#!/usr/bin/env bash
# Regenerates every image the guides use, from the model and the figure
# sources in src/. Needs OpenSCAD 2021+; on a machine with no display, run it
# under xvfb-run. Usage: ./make_renders.sh   (from this folder)
set -euo pipefail
cd "$(dirname "$0")"
MODEL=../amoled18_back_plate.scad
PRESETS=../amoled18_back_plate.json
DEFAULT="103035 1000mAh on edge (default)"
FLAT="802525 400mAh flat"
TALL="104050 2400mAh on end (tight, measure first)"

run() {
  local out=$1; shift
  if [ -n "${DISPLAY:-}" ]; then openscad --preview --colorscheme=Tomorrow -o "$out" "$@"
  else xvfb-run -a -s "-screen 0 1600x1200x24" openscad --preview --colorscheme=Tomorrow -o "$out" "$@"
  fi 2>&1 | grep -iE "warning|error" || true
  echo "  $out"
}
iso="--viewall --autocenter --imgsize=900,700"

echo "Overview"
run hero.png "$MODEL" $iso --camera=0,0,0,55,0,25,0 -D show_battery=true
run back.png "$MODEL" $iso --camera=0,0,0,235,0,25,0
run cutaway.png src/fig_cutaway.scad $iso --camera=0,0,0,70,0,70,0
run print-bed.png src/fig_print_bed.scad $iso --camera=0,0,0,55,0,20,0

echo "Versions (cut open, battery green, tape red, foam yellow)"
run version-103035.png src/fig_version.scad $iso --camera=0,0,0,60,0,300,0 -p "$PRESETS" -P "$DEFAULT"
run version-802525.png src/fig_version.scad $iso --camera=0,0,0,60,0,300,0 -p "$PRESETS" -P "$FLAT"
run version-104050.png src/fig_version.scad $iso --camera=0,0,0,60,0,300,0 -p "$PRESETS" -P "$TALL"

echo "Plugs"
run plugs.png "$MODEL" $iso --camera=0,0,0,50,0,30,0 -D 'part="plugs"'
run back-no-plugs.png src/fig_back_plugs.scad $iso --camera=0,0,0,235,0,25,0 -D fitted=0
run back-plugs.png src/fig_back_plugs.scad $iso --camera=0,0,0,235,0,25,0 -D fitted=1
run plug-close-open.png src/fig_back_plugs.scad --imgsize=900,700 --camera=-12,-18,0,215,0,35,38 -D fitted=0
run plug-close-fitted.png src/fig_back_plugs.scad --imgsize=900,700 --camera=-12,-18,0,215,0,35,38 -D fitted=1

echo "Labelled figures"
ortho="--projection=o --imgsize=1200,850"
run fig-outline.png src/fig_outline.scad --projection=o --imgsize=1100,1000 --viewall --autocenter --camera=0,0,200,0,0,0
run fig-battery-room.png src/fig_battery_room.scad --projection=o --imgsize=1100,1000 --viewall --autocenter --camera=0,0,200,0,0,0
run fig-rim-detail.png src/fig_rim_detail.scad $ortho --camera=0,21,32,90,0,90,46
run fig-floor-detail.png src/fig_floor_detail.scad $ortho --camera=0,20,3,90,0,90,46
run fig-tower-top.png src/fig_tower_top.scad $ortho --camera=0,20,33.5,90,0,90,50
run fig-tower-bottom.png src/fig_tower_bottom.scad $ortho --camera=0,19.5,3,90,0,90,54
for d in 0 0.3 0.5; do
  run "fig-grip-$d.png" src/fig_grip.scad --projection=o --imgsize=500,800 --camera=17,0,8.5,90,0,0,72 -D grip_depth=$d
done

echo "Desk stand"
HEAD="Desk stand: head (fits every box)"
run stand.png src/fig_stand.scad --imgsize=900,700 --camera=0,0,26,62,0,235,190 -p "$PRESETS" -P "$DEFAULT"
run stand-back.png src/fig_stand.scad --imgsize=900,700 --camera=0,0,35,62,0,125,220 -p "$PRESETS" -P "$DEFAULT" -D 'view="exploded"'
run stand-section-103035.png src/fig_stand.scad --projection=o --imgsize=900,800 --camera=0,0,34,90,0,180,190 -p "$PRESETS" -P "$DEFAULT" -D 'view="section"'
run stand-section-802525.png src/fig_stand.scad --projection=o --imgsize=900,800 --camera=0,0,26,90,0,180,190 -p "$PRESETS" -P "$FLAT" -D 'view="section"'
run stand-section-104050.png src/fig_stand.scad --projection=o --imgsize=900,800 --camera=0,0,44,90,0,180,250 -p "$PRESETS" -P "$TALL" -D 'view="section"'
run stand-head.png "$MODEL" --imgsize=900,700 --camera=0,0,2,40,0,20,120 -p "$PRESETS" -P "$HEAD"
run stand-box-top.png "$MODEL" --imgsize=900,700 --camera=0,0,24,35,0,250,200 -p "$PRESETS" -P "Desk stand: box for 103035 on edge"
