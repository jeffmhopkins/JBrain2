# Deep back plate — ESP32-S3-Touch-AMOLED-1.8

> **Status:** Living · **Last verified:** 2026-09-23

A 3D-printable replacement for the stock back cover of the
[Waveshare ESP32-S3-Touch-AMOLED-1.8](https://www.waveshare.com/esp32-s3-touch-amoled-1.8.htm) —
the panel that runs the room-endpoint pet firmware in
[`../../firmware/`](../../firmware/README.md).

**The stock cover only fits a tiny battery. This one is a deeper box that holds a real one**, so
the panel runs off the wall instead of off a cable. Everything else stays stock: all the ports,
buttons and the microphone are in the *front* shell, so this part has no openings except four
screw holes you can plug.

| Inside, battery ghosted in green | Back face |
|---|---|
| ![Render with the battery ghosted in green](preview.png) | ![Back face with the four screw openings](preview-back.png) |

---

## Where to go

| I want to… | Read |
|---|---|
| **Print one and fit it** — parts, settings, assembly | [**PRINTING.md**](PRINTING.md) |
| **Change something** — a different battery, a better fit | [**MODEL-ADJUSTMENT.md**](MODEL-ADJUSTMENT.md) |
| **It printed but something's wrong** | [**TROUBLESHOOTING.md**](TROUBLESHOOTING.md) |
| Understand why it's built this way, or check the numbers | [HOW-IT-WORKS.md](HOW-IT-WORKS.md) |

**If you just want the most likely one:** print
[`back_plate_103035_1000mAh_edge.stl`](back_plate_103035_1000mAh_edge.stl), flat face down, no
supports, PETG. Full details in [PRINTING.md](PRINTING.md).

---

## Safety — read before you build one

> **These go in small children's bedrooms.** Please read all four.

- **The hole plugs are a choking hazard for babies and toddlers.** They are about 5 mm across and
  a determined child can pry one out. For small children, glue them in — or leave them off
  entirely and untick `plug_recess` so the back prints plain.
- **Never pinch or compress a lithium pouch cell.** Keep the clearance the model allows. Don't
  fit a cell that is puffed, dented or damaged.
- **Check the battery plug's polarity** against the `+` and `−` marks by the board's `BAT` socket
  before connecting. Cheap cells don't agree on which wire is which.
- Don't leave it charging unattended.

---

## The three versions

| File | Battery | Finished unit |
|---|---|---|
| **`back_plate_103035_1000mAh_edge.stl`** (recommended) | 103035, 1000 mAh, on its edge | 37.6 × 45.2 × 44 mm |
| `back_plate_802525_400mAh_flat.stl` | 802525, 400 mAh, lying flat | 37.6 × 45.2 × 24.7 mm |
| `back_plate_104050_2400mAh_end.stl` | 104050, 2400 mAh, on end — **tight, [read first](HOW-IT-WORKS.md#the-tall-104050-version)** | 37.6 × 45.2 × 67 mm |
| `amoled18_hole_plugs.stl` (optional) | — | Six caps that hide the screw openings |

"Finished unit" is the whole device — display, front shell and this plate.

---

## Files

| File | What it is |
|---|---|
| `amoled18_back_plate.scad` | The parametric model. You never edit code; everything is a labelled field. |
| `amoled18_back_plate.json` | The three versions as Customizer presets. |
| `back_plate_*.stl` | Ready-to-print plates, one per version. |
| `amoled18_hole_plugs.stl` | Six press-fit plugs (four plus spares). |
| `preview*.png` | The renders used across these pages. |
| `photos/` | The real unit. |
| `reference/` | Waveshare's drawing, 3D model, schematic and web pages, plus the script that measured them. See [`reference/README.md`](reference/README.md). |

| Stock cover, inside | Assembled back | Board in the front shell |
|---|---|---|
| ![](photos/stock-back-cover-inside.jpg) | ![](photos/assembled-back.jpg) | ![](photos/board-in-front-shell.jpg) |

Waveshare's own resources: [wiki](https://www.waveshare.com/wiki/ESP32-S3-Touch-AMOLED-1.8) ·
[sample code](https://github.com/waveshareteam/ESP32-S3-Touch-AMOLED-1.8).
