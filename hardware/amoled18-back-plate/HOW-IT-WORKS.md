# How it works, and where the numbers come from

> **Status:** Living · **Last verified:** 2026-09-23

Background for when you want to know *why* a field exists, or whether a default can be trusted.
Nothing here is needed to print one — that's [PRINTING.md](PRINTING.md).

---

## The shape of it

The plate is a box with straight walls: the whole inside is open apart from four screw towers,
and **its depth follows the battery**. The cell sits on tape with foam above it; the cavity is
deliberately not shaped to the cell, so any gaps are packed with foam. All ports, buttons and the
mic are in the *front* shell, so the plate has no other openings.

![Cut through two screw towers](preview-cutaway.png)

### Battery cavity

- **Orientation.** Across the case the screws are only 24 mm apart, so a wide cell can't lie flat
  past them. Standing a cell on its **edge** makes it just 10 mm wide there; standing it on its
  **end** turns its length into the plate's depth, so any length fits.
- **Wire room.** A pouch cell's wires come out of one end. `lead_end` (2 mm) keeps that much free
  past the wire end; the default still has 1.85 mm spare at each end beyond that. A 40 mm cell
  such as an 852540 doesn't fit flat or on its edge once its wires have room.
- **Clearance rules.** The console warns `TIGHT` when anything is closer than 0.5 mm to the cell,
  and refuses to export under 0.2 mm, **because real cells often run half a millimetre over**.
- **Bracing.** A 45° chamfer runs where the walls meet the floor (`wall_chamfer`, 3 mm), and a 45°
  flare where each tower meets the floor (`tower_flare`, 2 mm). Both shrink by themselves near the
  cell, keeping 0.5 mm off its bottom edge, so they never lift it.
- **The board's `BAT` connector** stands 3.5 mm off the back of the board, on one long side about
  halfway along. The flat 802525 sits under it, which is why that version leaves 2 mm extra above
  the cell (`lead_space`).
- **The rim** that slides into the front shell is the top of the wall itself (0.8 mm thick), so it
  sits squarely on the wall with nothing hanging over air. Its opening sets the cavity size.

---

## Screws

The brass nuts the screws thread into are soldered to the **board**, not the case. On the stock
cover a post around each screw presses on its nut, so tightening the screws clamps the board.
This plate does the same with four hollow towers from the back face to the rim top:

- The bore (Ø4.6 mm) takes the screw head and hex key up to the **seat**: 2 mm of plastic at the
  top of the tower (`head_seat`). The head rests under the seat; the screw only passes through the
  seat and into the nut, **so short screws work at any plate height**.
- Only a Ø4.5 mm pad at the very top touches the board, standing 0.5 mm proud of the rest of the
  tower (`nut_pad_d`, `nut_pad_h`), because Waveshare's 3D model has small parts on the board
  about 3 mm from some nuts.
- The top of each bore is closed by one printed layer (`bridge_skin`, 0.2 mm) so the printer can
  bridge it. It's cleared before assembly.

### Choosing the length

Push a stock screw through the stock cover and measure how far it sticks out past its post —
that's how far it goes into the nut. Length = 2 mm (the seat) + that, rounded **down** to a length
that's sold.

**The display sits right against the front of the board**, so never exceed
2 mm + the nut's height + 1.2 mm (the board) − 0.5 mm.

Without a stock screw to measure, use **M2 × 4**; go to M2 × 5 only if the nuts are at least 2 mm
tall. The console's `Hex key reach` line gives the depth down each tower: about 33 mm for the
default, 11 mm for the flat version, 56 mm for the tall one.

---

## Hole plugs

Each tower opening on the back has a shallow recess (Ø5.6 × 0.8 mm). The plugs are thin caps with
a ribbed shank: the six ribs crush slightly as one goes in, for a firm press fit without glue, and
the cap sits flush. A small gap around each cap lets a knife tip pry it out to reach the screw.
`amoled18_hole_plugs.stl` has six — four plus spares.

| The six plugs | Back without plugs | Back with plugs fitted |
|---|---|---|
| ![The six hole plugs](preview-plugs.png) | ![Back face without plugs](preview-back-no-plugs.png) | ![Back face with plugs fitted](preview-back-plugs.png) |

> **Choking hazard for babies and toddlers.** See
> [Safety](README.md#safety--read-before-you-build-one).

---

## Grip

Horizontal ribs run right round the outside, corners included, so small hands don't drop it: bands
3 mm tall standing 0.3 mm proud (`grip_depth`, 0–0.5 mm, 0 for smooth), with 45° slopes top and
bottom **so they print without supports**. They stop 1.5 mm short of the bed edge and of the seam.
`grip_style` also offers `honeycomb` (raised hexagons) and `nubs` (raised squares) on the flat
sides.

---

## The tall 104050 version

A 104050 (10 × 40 × 50 mm, sold as 2400 mAh; expect about 2000) standing on its 10 × 40 face,
circuit board and wires at the top: roughly twice the default's capacity, in a unit about 67 mm
tall.

![The tall version with the cell ghosted in](preview-104050-end.png)

- **The fit is tight**: 40 mm in a 40.7 mm opening — 0.35 mm per end. Measure the real cell. Over
  40 mm, set `lip_wall` to 0.6, which opens the space to 41.1 mm.
- **The common listing ships a JST PH 2.0 mm plug**; swap it for a 1.25 mm one or use a short
  adapter. There's room beside the cell for either.
- **The screws are the same M2 × 4** — the towers take the height — but the hex driver needs a
  60 mm shaft to reach.

---

## Where the numbers come from

Sources: Waveshare's [dimension drawing](https://docs.waveshare.com/ESP32-S3-Touch-AMOLED-1.8) and
[3D model](https://files.waveshare.com/wiki/ESP32-S3-Touch-AMOLED-1.8/ESP32-S3-Touch-AMOLED-1.8-3D.zip)
(from the [resources page](https://docs.waveshare.com/ESP32-S3-Touch-AMOLED-1.8/Resources-And-Documents)),
and the photos in `photos/`. Copies are kept in [`reference/`](reference/README.md), with
`reference/measure.py` to re-derive the numbers. **The 3D model has the board and display but not
the case shells**, which is why several numbers below are estimates rather than measurements.

| Default | Value | Source | Confidence |
|---|---|---|---|
| `plate_x` × `plate_y` | 37.6 × 45.2 | Case outline, dimension drawing | High (official) |
| `screw_dx`, `screw_dy` | 12.0, 18.0 (24 × 36 apart) | PCB mounting holes in the 3D model; the drawing and all three photos agree within ~0.7 mm | High |
| `plate_r` | 8.7 | Circle fitted to the case corners in the drawing | Good, ±0.5 |
| `lip_outer_x` × `lip_outer_y`, `lip_outer_r` | 35.0 × 42.6, 7.4 | Outline minus a ~1.3 mm front-shell wall, from the photos (the board is 33.0 × 40.6) | Estimate: **measure** |
| `lip_h` | 2.0 | Not visible in any source | Guess: **measure** |
| `stock_clear` | 3.9 | The stock cover shows 3.5 mm in the side view; derived from that and the rim height | Estimate |
| `tower_drop` | 0 | Where the stock posts stop isn't visible in any source | Guess: **measure** |
| `lip_wall` | 0.8 | Chosen: the rim's inside is the cavity, and 0.8 mm leaves a 40.7 mm opening | Design choice |
| `screw_size` | M2 | Heads measure ~3.8 mm across in the drawing (the stock ones are Phillips) | Likely |
| Tower bore | Ø4.6 | ISO 4762 M2 head (Ø3.8) + `head_clear` 0.6 + `hole_slop` 0.2 | Standard |

The four marked **measure** are the ones worth checking against your own stock cover before a
keeper print — see [Measuring the stock cover](MODEL-ADJUSTMENT.md#measuring-the-stock-cover).

For reference: the whole stock unit is 15.0 mm thick; Waveshare's largest recommended cell for the
stock case is 3.85 × 24 × 28 mm; the stock back label window is 27.6 × 27.6 mm, R1.8. The stock
cover also has side rails and a raised block inside that this plate doesn't copy — on the test
fit, check nothing on the board (the speaker, for instance) was resting on them.

| Stock cover, inside | Assembled back | Board in the front shell |
|---|---|---|
| ![](photos/stock-back-cover-inside.jpg) | ![](photos/assembled-back.jpg) | ![](photos/board-in-front-shell.jpg) |
