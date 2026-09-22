# Reference files

> **Status:** Living · **Last verified:** 2026-09-22

Waveshare's published files for the ESP32-S3-Touch-AMOLED-1.8, kept here so the back plate's
defaults can be checked without depending on their site. Each one is downloaded unmodified from
the board's [resources page](https://docs.waveshare.com/ESP32-S3-Touch-AMOLED-1.8/Resources-And-Documents).

| File | What it is | Used for |
|---|---|---|
| `waveshare-dimensions.webp` | Official dimension drawing ([source](https://docs.waveshare.com/ESP32-S3-Touch-AMOLED-1.8)) | Case outline 37.6 × 45.2 × 15.0 mm, corner radius, back label window 27.6 × 27.6 R1.8 |
| `waveshare-3d-model.zip` | STEP model of the board and display, without the case shells ([source](https://files.waveshare.com/wiki/ESP32-S3-Touch-AMOLED-1.8/ESP32-S3-Touch-AMOLED-1.8-3D.zip)) | PCB 33.0 × 40.6 mm and its four mounting holes, 24 × 36 mm apart |
| `waveshare-schematic.pdf` | Board schematic ([source](https://files.waveshare.com/wiki/ESP32-S3-Touch-AMOLED-1.8/ESP32-S3-Touch-AMOLED-1.8.pdf)) | Battery connector and charging circuit |
| `measure.py` | Re-derives the numbers above from these files | Checking or updating the `.scad` defaults |

The STEP file opens in any CAD program (FreeCAD, Fusion 360, Onshape) — handy for checking
clearances between the battery pocket and the parts on the back of the board.

`measure.py` output, for comparison:

```
PCB: 33.01 x 40.62 x 1.20 mm
  hole centre: (-12.05, -18.05) mm from board centre
  hole centre: (-12.09, +18.10) mm from board centre
  hole centre: (+12.06, -18.05) mm from board centre
  hole centre: (+12.01, +18.00) mm from board centre
Case corner radius: 8.8 mm (fit error 0.23 px²)
```
