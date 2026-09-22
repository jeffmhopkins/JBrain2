# Reference files

> **Status:** Living · **Last verified:** 2026-09-22

Waveshare's published files for the ESP32-S3-Touch-AMOLED-1.8, kept here so the back plate's
defaults can be checked without depending on their site staying up. Every file here is an
unmodified copy of Waveshare's original; the table says where each one came from.

| File | What it is | Used for |
|---|---|---|
| `waveshare-dimensions.webp` | Official dimension drawing ([source](https://docs.waveshare.com/ESP32-S3-Touch-AMOLED-1.8)) | Case outline 37.6 × 45.2 × 15.0 mm, corner radius, back label window 27.6 × 27.6 R1.8 |
| `waveshare-3d-model.zip` | STEP model of the board and display, without the case shells ([source](https://files.waveshare.com/wiki/ESP32-S3-Touch-AMOLED-1.8/ESP32-S3-Touch-AMOLED-1.8-3D.zip)) | PCB 33.0 × 40.6 mm and its four mounting holes, 24 × 36 mm apart |
| `waveshare-schematic.pdf` | Board schematic ([source](https://files.waveshare.com/wiki/ESP32-S3-Touch-AMOLED-1.8/ESP32-S3-Touch-AMOLED-1.8.pdf)) | Battery connector and charging circuit |
| `pages/` | Saved copies of the web pages the numbers came from (see below) | Keeping the sources if Waveshare's site changes |
| `measure.py` | Re-derives the numbers above from these files | Checking or updating the `.scad` defaults |

## Saved web pages

Saved 2026-09-22. Open them in a browser; they read fine offline, though images other than
the dimension drawing aren't included.

| File | Page | How it was saved |
|---|---|---|
| `pages/docs-main.html` | [Waveshare docs: overview](https://docs.waveshare.com/ESP32-S3-Touch-AMOLED-1.8), with the dimension drawing and the MX1.25 battery connector | Direct download |
| `pages/docs-resources.html` | [Waveshare docs: resources](https://docs.waveshare.com/ESP32-S3-Touch-AMOLED-1.8/Resources-And-Documents), the download links above | Direct download |
| `pages/wiki.html` | [Waveshare wiki](https://www.waveshare.com/wiki/ESP32-S3-Touch-AMOLED-1.8): "recommended battery specification is 3.85\*24\*28 400mAh" | Wayback Machine snapshot from 2026-07-23 (the live site blocks scripted downloads) |
| `pages/product.html` | [Product page](https://www.waveshare.com/esp32-s3-touch-amoled-1.8.htm): "to fit inside the case, a battery size of 3.85 × 24 × 28 mm is recommended" | Wayback Machine snapshot from 2026-09-14 |

## Using the files

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
