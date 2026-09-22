# Deep back plate — ESP32-S3-Touch-AMOLED-1.8

> **Status:** Living · **Last verified:** 2026-09-22

A 3D-printable replacement for the stock back cover of the
[Waveshare ESP32-S3-Touch-AMOLED-1.8](https://www.waveshare.com/esp32-s3-touch-amoled-1.8.htm)
([wiki](https://www.waveshare.com/wiki/ESP32-S3-Touch-AMOLED-1.8),
[sample code](https://github.com/waveshareteam/ESP32-S3-Touch-AMOLED-1.8)) — the panel that
runs the room-endpoint pet firmware in [`../../firmware/`](../../firmware/README.md).

The stock cover leaves almost no room behind the board: Waveshare's largest recommended cell
for the stock case is 3.85 × 24 × 28 mm. This plate is a tall box with straight walls: the
whole inside is open, and its depth follows the cell. The four screws run bare through it. Any gaps are
padded with foam. The cell plugs into the board's MX1.25 `BAT` connector. Everything else about
the case stays stock: all ports, buttons and the mic are in the **front** shell, so the plate
has no other openings.

The default is a **1000 mAh 852540 cell (8.5 × 25 × 40 mm) standing on its long edge**, which
makes the unit about **37.6 × 45.2 × 39 mm**, close to a cube. A long cell can't lie flat: the
screws pass through 24 mm apart across the case, so a 25 mm wide cell would sit across them,
but a cell on its edge is just 8.5 mm wide there. Smaller cells can lie flat instead (`battery_orientation = flat`); an 802525 lying
flat makes the unit about 22.7 mm thick.

The back face is flat, with only a 0.4 mm chamfer on its edge so it prints cleanly face down.
The screw holes are counterbored so standard M2 socket head cap screws (the hex-key kind) sit
just below the surface.

| Inside, battery ghosted in green | Back face |
|---|---|
| ![Render with the battery ghosted in green](preview.png) | ![Flat back face with counterbored screw holes](preview-back.png) |

## Files

| File | What it is |
|---|---|
| `amoled18_back_plate.scad` | The parametric model. Every dimension is adjustable. |
| `back_plate_852540_1000mAh_edge.stl` | Ready to print: the defaults, for an 852540 cell on its edge. Unit ≈ 39 mm thick. |
| `back_plate_802525_400mAh_flat.stl` | Ready to print for an 802525 cell lying flat. Unit ≈ 22.7 mm thick. |
| `back_plate_104050_2400mAh_end.stl` | **Variation:** a 104050 cell standing on its end. Unit ≈ 67 mm tall. See [The tall variation](#the-tall-variation-104050-on-end). |
| `amoled18_back_plate.json` | The three builds above as presets, selectable in OpenSCAD's Customizer. |
| `preview.png`, `preview-back.png`, `preview-104050-end.png` | Renders of the inside (battery ghosted in), the back face, and the tall variation. |
| `photos/` | The real unit: the stock cover's inside, the assembled back, the board in the front shell. |
| `reference/` | Archived copies of Waveshare's drawing, 3D model, schematic and web pages, plus the script that measured them. See [`reference/README.md`](reference/README.md). |

## Step by step, if you've never used OpenSCAD

OpenSCAD is a free CAD program where the shape is described by numbers rather than drawn by
hand. The upside is that you never have to model anything: you change a number in a form
("battery thickness = 8.6"), and the whole part redraws itself to fit. This file was written
that way on purpose, so **you should never need to touch the code** — only the form.

The `.stl` in this folder is built from the defaults in [Where the numbers come
from](#where-the-numbers-come-from). The outline and screw holes come from Waveshare's own
drawings; the rim and depth are estimates. Print it as a first test, then use the steps below
to adjust anything that doesn't fit.

### 1. Get the files

On the GitHub page for this folder, click `amoled18_back_plate.scad`, then the **download**
button (the down-arrow icon, top right of the file view). Download `amoled18_back_plate.json`
the same way and keep it in the same folder: it holds the ready-made presets. The `.stl` files
are pre-made exports.

### 2. Install OpenSCAD

Download it free from [openscad.org](https://openscad.org/downloads.html) (Windows, Mac,
Linux) and install it like any other program.

### 3. Open the file and find the form

1. Open OpenSCAD, choose **Open**, and pick `amoled18_back_plate.scad`.
2. The window has three parts: the **code** on the left (ignore it), the **3D view** in the
   middle, and — after the next step — the **Customizer** form on the right.
3. If there is no form, go to **Window → Customizer** (on some versions, **View → Hide
   customizer** is ticked — untick it).
4. Press **F5**. The plate appears in the 3D view. Drag to spin it, scroll to zoom.

The form has numbered groups — click a group name to expand it. Each field has a plain-English
label, and most are sliders or drop-downs, so you can't enter something wildly wrong.

### 4. Check the numbers against the stock cover

The outline and screw positions come from Waveshare's own drawings, so they should already be
right. The **rim** and **depth** values are estimates from photos. Before a print you intend to
keep, measure the original black back cover and fix anything that's off. Digital calipers are
ideal; a good ruler works for a first try. All values are in millimetres.

| Form group | Field | What to measure on the stock cover |
|---|---|---|
| 2. Plate outline | `plate_x`, `plate_y` | Overall outside width and height |
| 2. Plate outline | `plate_r` | Roughly how round the outer corners are (radius) |
| 3. Rim | `lip_outer_x`, `lip_outer_y` | Outside width and height of the raised rim that slides into the front shell |
| 3. Rim | `lip_h` | How tall that rim stands |
| 3. Rim | `stock_clear` | Inside depth of the stock cover, rim top down to the floor |
| 4. Screws | `screw_dx`, `screw_dy` | Distance from the **centre** of the plate to the centre of a screw hole, across and up. Easiest: measure hole-to-hole and halve it. |

### 5. Enter the battery you actually have

In **group 1 (Battery)** type the cell's thickness, width and length — they're usually on the
cell's label or listing (the size code reads thickness, width, length: "852540" is
8.5 × 25 × 40 mm). Measure the real cell if you can, including the little circuit board folded
at the wire end. Then pick `battery_orientation`:

- **edge** — standing on its long edge. Use this for long cells (35–40 mm).
- **flat** — lying on its face. Fine for small square cells like 802525.
- **end** — standing upright on its end. Any length fits; the plate just gets taller.

Set `tape_t` and `foam_t` to the thickness of the tape and foam you'll use, and `lead_space` to
the room the wires and plug need above the cell (3 mm is plenty; 0 for cells whose wires come
out the side). The plate's depth follows automatically.

Shortcut: the drop-down at the top of the Customizer has the three builds in this folder as
presets. Pick one and every battery setting fills in.

### 6. Check the console for warnings

Press **F5** again after changing values. At the bottom of the window is the **console**
(if it's hidden: **Window → Console**). It prints a summary block, and the last line should
say **`All checks passed.`** If instead it says something like `CELL SITS ON A SCREW PAD`,
the battery is too big for this plate — pick a smaller cell or adjust the flagged value.

Write down the **`SCREWS:`** line — it gives the reach you need to work out which
screws to buy (see [Screws](#screws)).

### 7. Export the file for the printer

1. Press **F6** to do the full render. This can take a minute; wait until the progress bar
   finishes and the console says it's done.
2. Press **F7** (or **File → Export → Export as STL**) and save it.

F5 is just a quick preview; export only works after F6.

### 8. Slice and print

Open the exported `.stl` in your usual slicer (Bambu Studio, PrusaSlicer, Cura, …) and use the
settings in [Printing](#printing) below: flat face down, rim up, no supports.

**Do a test print first.** Print one quickly and hold it against the front shell to check the
rim slides in and the four holes line up with the brass inserts. If something is off, measure
again, change the number, and re-export — that's the whole point of the parametric file.

### 9. Save your numbers

The form remembers your values only while the file is open. To keep them, use the **preset**
bar at the top of the Customizer: click **+**, name it (e.g. `my 802525`), and it's saved
alongside the `.scad` file.

## Where the numbers come from

Sources: Waveshare's [dimension drawing](https://docs.waveshare.com/ESP32-S3-Touch-AMOLED-1.8)
and [3D model](https://files.waveshare.com/wiki/ESP32-S3-Touch-AMOLED-1.8/ESP32-S3-Touch-AMOLED-1.8-3D.zip)
(from the [resources page](https://docs.waveshare.com/ESP32-S3-Touch-AMOLED-1.8/Resources-And-Documents)),
and the photos in `photos/`. Copies of Waveshare's files are in [`reference/`](reference/README.md),
with `reference/measure.py` to re-derive the numbers. The 3D model has the board and display
but not the case shells.

| Default | Value | Source | Confidence |
|---|---|---|---|
| `plate_x` × `plate_y` | 37.6 × 45.2 | Case outline, dimension drawing | High (official) |
| `screw_dx`, `screw_dy` | 12.0, 18.0 (24 × 36 apart) | PCB mounting holes in the 3D model. The drawing and all three photos agree to within ~0.7 mm. | High |
| `plate_r` | 8.7 | Circle fitted to the case corners in the dimension drawing | Good, ±0.5 |
| `lip_outer_x` × `lip_outer_y`, `lip_outer_r` | 35.0 × 42.6, 7.4 | Outline minus a ~1.3 mm front-shell wall, estimated from the photos. (The board itself is 33.0 × 40.6.) | Estimate: **measure** |
| `stock_clear` | 3.9 | The stock cover shows 3.5 mm on the side view; depth derived from that and the rim height | Estimate: **measure** |
| `lip_h` | 2.0 | Not visible in any source | Guess: **measure** |
| `lip_wall` | 0.8 | Chosen, not measured: the walls run straight up to the rim, so the rim's inside is the cavity, and 0.8 mm leaves a 40.7 mm opening for a 40 mm cell. Two nozzle widths, plenty for a 2 mm locating lip. | Design choice |
| `screw_size` | M2 | Screw heads measure ~3.8 mm across in the drawing, which matches M2 | Likely: check a stock screw |
| Counterbores | Ø4.6 × 2.3 deep | ISO 4762 M2 socket head (Ø3.8 × 2.0) + `head_clear` 0.6 + `hole_slop` 0.2, sunk `head_sink` 0.3 | Standard |

For reference, the whole stock unit is 15.0 mm thick, and the back label window is
27.6 × 27.6 mm with R1.8 corners.

## Screws

Use **M2 socket head cap screws** (ISO 4762 / DIN 912 — the round head with a hex-key
socket). The counterbores are sized for them with room for the key; other sizes in the
`screw_size` drop-down get matching counterbores.

There are no screw towers: each screw runs bare through the open inside, straight to the
case. Its head sits in a pad on the floor, because the floor alone is thinner than the
recess. Each pad is 8.6 mm across and 4.3 mm tall, with 2 mm of plastic over the head
(`head_seat`) and around it (`pad_wall`). The screws pull the plate's rim against the front shell. The deeper
plate means the stock screws are far too short. To work out the length:

1. The console's `SCREWS:` line gives the **reach**: from where the head sits up to the rim top.
   It's 27.3 mm with the defaults (10.9 mm for the flat 802525 version).
2. Push a stock screw through the stock cover and measure how far it sticks out past the top of
   its screw post. That's how far it goes into the case.
3. Screw length = reach + that, rounded **down** to a length that's sold, so it can't bottom out.

If you can't measure a stock screw: 2–3 mm into the case is typical, which makes it M2 × 30
for the default plate and M2 × 12 for the flat 802525 one. Long M2 socket head screws are sold
on their own; small assortments usually stop at 20 mm. The tall variation needs about 58 mm,
which is covered below.

## The tall variation: 104050 on end

A 104050 cell (10 × 40 × 50 mm, sold as 2400 mAh; expect about 2000) standing on its 10 × 40
face, circuit board and wires at the top. The unit comes out about **37.6 × 45.2 × 67 mm**, with
roughly twice the capacity of the default and 5–6× the 802525. Select the
`104050 2400mAh on end` preset, or print `back_plate_104050_2400mAh_end.stl`.

![The tall variation with the cell ghosted in](preview-104050-end.png)

Things to know before building it:

- **The fit along the case is tight:** 40 mm in a 40.7 mm opening, the same 0.35 mm per end as
  the default. Measure the real cell. If it's over 40 mm, set `lip_wall` to 0.6, which opens
  the space to 41.1 mm.
- **The common listing has the wrong plug.** It ships with a JST PH 2.0 mm plug; the board
  needs MX1.25 (1.25 mm). Swap the plug for a 1.25 mm two-pin one, or use a short PH 2.0 to
  Micro JST 1.25 adapter; there's room beside the cell for either. Check which pin is + and
  which is − against the board's `BAT` marks before plugging in.
- **The screws are about 58 mm long** (reach 55.3 mm + ~2.5 into the case). M2 socket head
  screws that long are hard to find, so search for them before printing. The fallback is M2
  threaded rod cut to length: screw it into the case's insert, then tighten an M2 nut on the
  back with a 4 mm nut driver. A round counterbore can't hold a nut still, so for that route
  untick `counterbore` and let the nut and a washer sit on the flat back face.

## Battery mounting

The walls are braced by a 45° chamfer where they meet the floor, all the way around the inside
(`wall_chamfer`, 3 mm). On any wall the cell sits close to, it shrinks by itself so it never
lifts the cell. With the default cell on edge, that's 3 mm on the long walls and 1.25 mm on the
end walls; OpenSCAD's console prints both.

The cell is held by double-sided VHB tape on the floor and foam padding above it, which presses
it down when the case closes. The cavity is deliberately open rather than shaped to the cell;
pack foam into the gaps around it so it can't shift. Set `tape_t` and `foam_t` to what you
actually have; both add to the depth. There's room at the ends and beside the cell for the wires
to reach the `BAT` connector.

## Printing

| Setting | Value |
|---|---|
| Orientation | Flat face down, rim up. No supports. |
| Counterbores | The shelf at the top of each one is a short bridge; it prints fine at 0.2 mm. |
| Layer | 0.2 mm |
| Perimeters | 3 or more (the walls and screw pads take the load) |
| Infill | 40 %+ |
| Material | PETG or ABS — PLA softens in a warm car. |

## Safety

These go to kids. Never pinch or compress a lithium pouch cell — keep the clearance the model
allows. Don't install a cell that is puffed, dented or damaged, and don't leave one charging
unattended.

## Photos

| Stock cover, inside | Assembled back | Board in the front shell |
|---|---|---|
| ![](photos/stock-back-cover-inside.jpg) | ![](photos/assembled-back.jpg) | ![](photos/board-in-front-shell.jpg) |
