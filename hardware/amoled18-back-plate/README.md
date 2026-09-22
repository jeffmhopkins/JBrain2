# Deep back plate — ESP32-S3-Touch-AMOLED-1.8

> **Status:** Living · **Last verified:** 2026-09-22

A 3D-printable replacement for the stock back cover of the
[Waveshare ESP32-S3-Touch-AMOLED-1.8](https://www.waveshare.com/esp32-s3-touch-amoled-1.8.htm)
([wiki](https://www.waveshare.com/wiki/ESP32-S3-Touch-AMOLED-1.8),
[sample code](https://github.com/waveshareteam/ESP32-S3-Touch-AMOLED-1.8)) — the panel that
runs the room-endpoint pet firmware in [`../../firmware/`](../../firmware/README.md).

The stock cover leaves almost no room behind the board, so only a very thin cell fits. This
plate is deeper and has a pocket sized to a real LiPo (the sample export is for an 802525,
~400 mAh), plugged into the board's `BAT` connector. Everything else about the case stays
stock: all ports, buttons and the mic are in the **front** shell, so this part is solid except
for the battery pocket and four screw holes.

![Render with the battery ghosted in green](preview.png)

## Files

| File | What it is |
|---|---|
| `amoled18_back_plate.scad` | The parametric model. Every dimension is adjustable. |
| `back_plate_802525_400mAh.stl` | A sample export. **Placeholder dimensions — see below.** |
| `preview.png` | Render with the battery ghosted in. |
| `photos/` | The real unit: the stock cover's inside, the assembled back, the board in the front shell. |

## Measure before printing one you intend to keep

The outline, rim and screw spacing in the file are **estimates**. Before a keeper print,
measure the stock back cover and correct group 2 (plate outline), group 3 (rim) and group 4
(screw spacing). A quick first print to check that the screw holes line up is cheap insurance.

## Adjusting it

1. Install [OpenSCAD](https://openscad.org) (free).
2. Open `amoled18_back_plate.scad`.
3. **Window → Customizer** — the parameters show as labelled fields in six groups; no code
   editing needed.
4. Set the battery size (group 1) to the cell you actually have, including `tape_t` and
   `foam_t`. The console prints the resulting depth and warns if the pocket overlaps a screw
   hole or the rim doesn't fit.
5. **F5** preview, **F6** render, **F7** export STL.

## Screws

Deepening the plate means the stock screws no longer reach the brass inserts in the board. The
OpenSCAD console prints exactly how much longer they must be (`SCREWS: M2, … mm longer than
stock`). Buy M2 screws of that length.

## Battery mounting

The cell sits on double-sided VHB tape on the pocket floor with foam padding above it — no
clip. Set `tape_t` and `foam_t` to what you actually have; both add to the required depth.

## Printing

| Setting | Value |
|---|---|
| Orientation | Flat face down, rim up. No supports. |
| Layer | 0.2 mm |
| Perimeters | 3 or more (the screw columns take the load) |
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
