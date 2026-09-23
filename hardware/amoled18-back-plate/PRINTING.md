# Printing and fitting the back plate

> **Status:** Living · **Last verified:** 2026-09-23

Everything needed to go from an empty printer to a finished panel. If a pre-made file doesn't
suit your battery, see [MODEL-ADJUSTMENT.md](MODEL-ADJUSTMENT.md) first. If it's printed and
something's wrong, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

| Inside, battery ghosted in green | Back face, as it prints |
|---|---|
| ![Render with the battery ghosted in green](preview.png) | ![Back face with the four screw openings](preview-back.png) |

---

## 1. Pick a version

| File | Battery | Finished unit | Screws |
|---|---|---|---|
| **`back_plate_103035_1000mAh_edge.stl`** (recommended) | 103035, 1000 mAh, 10 × 30 × 35 mm, standing on its edge | 37.6 × 45.2 × 44 mm | M2 × 4 |
| `back_plate_802525_400mAh_flat.stl` | 802525, 400 mAh, 8 × 25 × 25 mm, lying flat | 37.6 × 45.2 × 24.7 mm | M2 × 4 |
| `back_plate_104050_2400mAh_end.stl` | 104050, 2400 mAh, 10 × 40 × 50 mm, on end — **tight fit, [read this first](HOW-IT-WORKS.md#the-tall-104050-version)** | 37.6 × 45.2 × 67 mm | M2 × 4 |
| `amoled18_hole_plugs.stl` (optional) | — | Six caps that hide the screw openings. **Not for babies — [choking hazard](README.md#safety--read-before-you-build-one).** | — |

"Finished unit" is the whole device: display, front shell and this plate. The grip ribs add
0.3 mm on each side.

---

## 2. Get the parts

- **The battery.** A 3.7 V single-cell LiPo with a **1.25 mm two-pin plug** (sold as "MX1.25",
  "Micro JST 1.25" or "PicoBlade"). A 2.0 mm "JST PH" plug **will not fit the board** — swap the
  plug or use an adapter.
- **4 × M2 × 4 socket head cap screws** (ISO 4762 / DIN 912 — the round head with a hex socket).
  Don't use longer ones without reading [Screws](HOW-IT-WORKS.md#screws): a screw that's too long
  presses on the display.
- **A 1.5 mm hex key or driver** that reaches about **35 mm down a 4.6 mm hole** (60 mm or more
  for the tall version). A screwdriver-style hex driver is easiest. *This catches people out — a
  stubby key will not reach the screw.*
- **1 mm double-sided VHB tape** and **1.5 mm foam** (the thin craft or gasket kind).
- Optional: a **2 mm drill bit**, to clear the thin printed layer in each screw tower.

You'll also want the **stock back cover** to hand — it's what you measure against if anything
needs adjusting.

---

## 3. Print it

| Setting | Value |
|---|---|
| **Orientation** | Flat back face **down** on the bed, rim **up**. |
| **Supports** | **None.** The part is designed so nothing needs them. |
| Material | PETG (or ABS). **Not PLA** — it softens in a hot car. |
| Layer height | 0.2 mm |
| Walls / perimeters | 3 or more. The rim is only 0.8 mm, so check the slicer preview shows it **solid (2 lines)**, not two walls with a gap. |
| Infill | 40 % or more |
| Plugs | Print as laid out in their file, flat **caps down**, so the face that shows is the smooth one off the bed. |

**Print a quick test one before a keeper** and try it on the unit (step 4). If anything is off,
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) says which number to change.

---

## 4. Put it together

### 4.1 Clear the screw towers — do this first

Each tower's hole is closed at the top by **one thin printed layer**, deliberately: it lets the
printer bridge the hole cleanly. Poke it through with a 2 mm drill turned by hand, or with the
screw itself.

> Skip this and the screws feel like they're binding, and you'll think the holes printed wrong.

### 4.2 Test-fit the plate, with no battery in it

The rim should slide into the front shell **without forcing**, and the four holes should line up
with the four brass nuts on the board.

Check too that nothing on the board — the speaker especially — was resting on the stock cover's
internal side rails, which this plate deliberately doesn't copy.

![Board in the front shell](photos/board-in-front-shell.jpg)

### 4.3 Check the battery's polarity — before plugging anything in

Against the `+` and `−` marks by the board's `BAT` socket. **Cheap batteries don't agree on which
wire is which**; if it's reversed, swap the two pins in the plug with a needle.

### 4.4 Tape the battery down

VHB on the floor of the plate, battery on top, **wires toward the end with the most room**. Trim
the tape so it lies flat rather than climbing the sloped edges at the walls.

### 4.5 Plug it in and pack it

Lay the foam on top of the cell and pack a little into any gaps so it can't shift. The cavity is
deliberately not shaped to the cell — **the foam is what holds it still.**

Keep the cell and its foam clear of the `BAT` connector, which stands 3.5 mm off the back of the
board on one long side, about halfway along.

### 4.6 Close it up

Seat the plate on the front shell, drop each screw down its tower, and tighten with the hex key
until snug. **Don't crank it** — the tower tops press on the board.

![Assembled back](photos/assembled-back.jpg)

### 4.7 Plugs (optional)

Press one into each opening on the back until flush. A knife tip in the small gap around a cap
pries it back out when you need the screw again.

| Back without plugs | Back with plugs fitted | The six plugs |
|---|---|---|
| ![Back face without plugs](preview-back-no-plugs.png) | ![Back face with plugs fitted](preview-back-plugs.png) | ![The six hole plugs](preview-plugs.png) |

> **Not around babies or toddlers.** See [Safety](README.md#safety--read-before-you-build-one).

---

## What to check before you call it done

- The rim is seated all round with no gap at the seam.
- The screws are snug, and the display shows no sign of being pressed from behind.
- The cell cannot move when you shake it gently.
- Nothing is resting on the `BAT` connector.

Anything not right → [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
