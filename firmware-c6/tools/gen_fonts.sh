#!/usr/bin/env bash
# Regenerates the vendored bitmap fonts in components/ui_kit/fonts/ (README "Fonts").
#
#   firmware-c6/tools/gen_fonts.sh <lvgl-v9.2.2-checkout>
#
# The TTFs are the ones LVGL v9.2.2 ships in its own tree — Montserrat-Medium is what its
# built-in fonts were cut from, Montserrat-Bold is the clock's 700 weight — so a regenerated
# file can only differ if lv_font_conv does. The sim's checkout (sim/build/_deps/lvgl-src) or
# the device build's managed_components/lvgl__lvgl both work.
set -euo pipefail
lvgl="${1:?path to an LVGL v9.2.2 checkout}"
here="$(cd "$(dirname "$0")/.." && pwd)"
out="$here/components/ui_kit/fonts"
conv=(npx --yes lv_font_conv@1.5.3)
medium="$lvgl/scripts/built_in_font/Montserrat-Medium.ttf"
bold="$lvgl/demos/multilang/assets/fonts/Montserrat-Bold.ttf"
mkdir -p "$out"

# The clock face (124 px) and the ringing alarm (92 px): digits, colon, and the dash the face
# shows before the panel knows the time. Nothing else, so each costs only its twelve glyphs.
for px in 124 92; do
  "${conv[@]}" --font "$bold" -r 0x2D,0x30-0x3A --size "$px" --bpp 4 --no-compress \
    --format lvgl --lv-include lvgl.h --lv-font-name "ui_font_clock_$px" \
    -o "$out/ui_font_clock_$px.c"
done
# The marks the copy uses that LVGL's built-in Montserrat lacks: · – — … → −. Each size is a
# fallback hung off the built-in font of the same size (ui_kit.c, ui_font).
for px in 14 16 18 20 22 24 26 28 30; do
  "${conv[@]}" --font "$medium" -r 0xB7,0x2013,0x2014,0x2026,0x2192,0x2212 --size "$px" --bpp 4 \
    --no-compress --format lvgl --lv-include lvgl.h --lv-font-name "ui_glyphs_$px" \
    -o "$out/ui_glyphs_$px.c"
done
# lv_font_conv records its arguments in each file's header; the machine-local paths in them
# would make a regeneration elsewhere a diff in every file.
sed -i -e "s|$lvgl/|<lvgl-v9.2.2>/|g" -e "s|$out/||g" "$out"/*.c
