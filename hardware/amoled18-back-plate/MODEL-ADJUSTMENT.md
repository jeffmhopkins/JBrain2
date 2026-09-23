# Changing the model

> **Status:** Living · **Last verified:** 2026-09-23

You need this if a pre-made file doesn't suit your battery, or if
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) told you to change a number.

**You never edit code.** The model is a form of labelled fields — you type a number, press a key,
and the whole part redraws itself around it. That is the entire skill required.

---

## First time: open it and export something

### 1. Install OpenSCAD

Free, from [openscad.org](https://openscad.org/downloads.html) — Windows, Mac, Linux. Install it
like anything else.

### 2. Get both files, into the same folder

Download `amoled18_back_plate.scad` **and** `amoled18_back_plate.json` from this folder (on
GitHub: click the file, then the download icon). **Keep them together** — the `.json` holds the
ready-made versions and OpenSCAD looks for it beside the model.

### 3. Open it and turn the form on

Open the `.scad` in OpenSCAD, then **Window → Customizer** (on some versions, untick
**View → Hide customizer**). Press **F5** to see the part.

You'll have three areas: code on the left — **ignore it entirely** — the 3D view in the middle,
and the form on the right. Drag to spin, scroll to zoom.

### 4. Start from a version that already exists

The drop-down at the top of the Customizer has all three builds. **Pick the closest one first**
and every field fills in at once — then change only what you need. Starting from blank is the
hard way round.

### 5. Read the console after every change

Press **F5**, then look at the console along the bottom (**Window → Console** if hidden). It ends
with one of:

| It says | Meaning |
|---|---|
| **`All checks passed.`** | Good. |
| **`TIGHT: …`** | It fits the cell's *listed* size with under 0.5 mm spare. Real cells often run half a millimetre over — **measure yours**. |
| **`CELL DOES NOT FIT`** | Too big for these settings. OpenSCAD will refuse to export. |

The console also prints every clearance the model worked out, including the `Hex key reach` down
each tower — worth reading before you buy a driver.

### 6. Export

1. **F6** — full render. Can take a minute; wait for it.
2. **F7** — save the `.stl`.

**F5 is only a preview; export won't work until you've done F6.**

### 7. Keep your numbers

The form forgets them when you close the file. Click **+** on the preset bar at the top of the
Customizer and name them (`my 802525`) — they're saved beside the `.scad`.

---

## What to change for what you want

| You want to… | Change | Group |
|---|---|---|
| Use a different battery | `battery_t`, `battery_w`, `battery_l` — the size code reads thickness, width, length (103035 = 10 × 30 × 35) | 1. Battery |
| Stand it up, lay it flat, or stand it on end | `battery_orientation`: **edge** for cells up to ~37 mm, **flat** for small square cells, **end** for long cells (the plate grows taller) | 1. Battery |
| Allow for different tape or foam | `tape_t`, `foam_t` | 1. Battery |
| More room for the battery's wires | `lead_end` (past the wire end of a flat or edge cell), `lead_space` (above a cell on end) | 1. Battery |
| Match a measured stock cover | `plate_x`, `plate_y`, `plate_r`, the rim (`lip_…`), `screw_dx`, `screw_dy`, `tower_drop` — see [below](#measuring-the-stock-cover) | 2, 3, 4 |
| Make the rim fit tighter or looser | `lip_slop` (bigger = looser) | 3. Rim |
| Screw heads or hex key too tight in the towers | `hole_slop` (bigger = bigger holes) | 4. Screws |
| Towers pressing too hard, or not reaching, the board | `tower_drop` (bigger = shorter towers; negative = taller than the rim) | 4. Screws |
| Different screws | `screw_size` | 4. Screws |
| Plugs looser or tighter | `plug_interference` (bigger = tighter) | 4c. Hole plugs |
| No plug recesses | untick `plug_recess` | 4c. Hole plugs |
| Stronger or weaker grip ribs | `grip_depth`, 0 to 0.5 mm (0 = smooth walls) | 5b. Grip |
| A different grip pattern | `grip_style`: ribs, honeycomb, nubs, none | 5b. Grip |
| Export the plugs instead of the plate | `part`: plate, plugs, or both | 6. Output |
| See the battery in the preview | tick `show_battery` | 6. Output |

### The ones you'll actually touch

A first build changes **the three battery dimensions and `battery_orientation`** — and, if the
test fit isn't right, `lip_slop` and `tower_drop`. Everything else in the form exists so the
model can be re-derived from Waveshare's drawings, not because you need it.

If you find yourself reaching for something not in the table above, read the console first. It is
usually already telling you the real problem.

---

## Measuring the stock cover

The outline and screw positions come from Waveshare's own drawings and should already be right.
A few numbers **couldn't be seen in any source and are estimates** — measuring these on the
original black cover (calipers ideal, a ruler works) is what makes a keeper print fit first time.

| Measure on the stock cover | Put it in |
|---|---|
| How tall the rim that slides into the front shell stands | `lip_h` |
| Outside width and length of that rim | `lip_outer_x`, `lip_outer_y` |
| Rim top down to the tops of the screw posts (0 if level; negative if the posts stand higher) | `tower_drop` |
| Inside depth, rim top down to the floor | `stock_clear` (only affects the reported numbers) |
| Hole to hole, across and along — **then halve each** | `screw_dx`, `screw_dy` |
| How far a stock screw sticks out past its post, and how tall the brass nuts stand off the board | Choosing screw length — see [Screws](HOW-IT-WORKS.md#screws) |

![Stock cover, inside](photos/stock-back-cover-inside.jpg)

---

## What the numbers are shaping

A cut through two screw towers, which is the clearest view of what most of the fields control —
the rim at the top, the tower bore and its seat, the floor chamfer, and the cavity the battery
lives in:

![Cut through two screw towers](preview-cutaway.png)

For what each part is for and why it's built that way, see
[HOW-IT-WORKS.md](HOW-IT-WORKS.md).
