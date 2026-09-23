# It printed, but something's wrong

> **Status:** Living · **Last verified:** 2026-09-23

Find your symptom below. Most fixes are one number in the Customizer — see
[MODEL-ADJUSTMENT.md](MODEL-ADJUSTMENT.md) for how to open it, change it and export again.

**Change numbers in small steps** (0.1–0.2 mm) unless something is obviously far out.

---

## Check these two first

Between them they explain most "it doesn't fit" reports, and neither needs a reprint.

1. **Did you clear the four screw towers?** Each tower's hole is closed at the top by one thin
   printed layer, on purpose. Poke it through with a 2 mm drill or the screw. **Un-cleared, the
   screws feel exactly like a bore that printed too small.**
2. **Did the rim print solid?** It's 0.8 mm — two lines. If the slicer drew one, it's weak and
   undersized. See [the rim row](#the-print-itself) below.

---

## Fitting the case

| Problem | Likely cause | Fix |
|---|---|---|
| **Rim won't go into the front shell** | Rim a little too big | Raise `lip_slop` by 0.1, or measure the rim and lower `lip_outer_x` / `lip_outer_y` |
| **Plate wobbles or rattles on the shell** | Rim a little small | Lower `lip_slop` by 0.1 (not below 0), or raise `lip_outer_x` / `lip_outer_y` |
| **Holes don't line up with the brass nuts** | Screw spacing | Measure hole to hole on the stock cover and set `screw_dx` / `screw_dy` to **half** of each |
| **Gap at the seam when the screws are tight**, or the board feels pushed forward | Towers too tall | Measure the gap and raise `tower_drop` by that much |
| **Board rattles, or the screws pull it backwards** | Towers too short to reach the nuts | Lower `tower_drop` (it can go negative) by the gap you see |
| **Something on the board is being pressed** | The stock cover has internal side rails and a raised block that this plate doesn't copy | Check what was resting on them — the speaker is the usual one |

![Cut through two screw towers](preview-cutaway.png)

---

## Screws

| Problem | Likely cause | Fix |
|---|---|---|
| **Screw won't go down the tower**, or the hex key jams | Thin top layer not cleared, or bore printed small | Clear the top layer with a 2 mm drill; if the bore itself is tight, raise `hole_slop` by 0.1 |
| **Screw spins without gripping** | Screw too short | Next length up — but stay under the limit in [Screws](HOW-IT-WORKS.md#screws) |
| **Screw feels like it hits something**, or the display looks pressed | **Screw too long** | **Stop.** Use a shorter screw — the tip is reaching the display |
| **Hex key won't reach the screw** | Key too short | The console prints `Hex key reach`: ~33 mm default, 56 mm for the tall version. A screwdriver-style driver with a 60 mm shaft covers all of them |

---

## Battery

| Problem | Likely cause | Fix |
|---|---|---|
| **Battery won't fit** | Cell bigger than its listing, or wires in the way | Measure the cell **including the folded circuit board at the wire end**, enter it, check the console; or raise `lead_end` |
| **Console says `CELL DOES NOT FIT`** | Too big for these settings | Measure the real cell; try `battery_orientation` = `edge`, then `end` |
| **Console says `TIGHT:`** | Under 0.5 mm spare against the *listed* size | Measure yours before printing — real cells often run 0.5 mm over |
| **Battery slides around** | Too much space | More foam. That's what it's for — the cavity is deliberately not shaped to the cell |
| **Foam or battery pressing on the board** | Too little headroom | Thinner foam, or raise `lead_space` by 1–2 mm |
| **Plug won't fit the board** | It's a 2.0 mm JST PH; the board needs 1.25 mm | Swap the plug for a 1.25 mm two-pin, or use a short adapter |
| **Unit too thick** | Battery choice | Use the flat 802525 version (24.7 mm) |

---

## The print itself

| Problem | Likely cause | Fix |
|---|---|---|
| **Rim prints thin or broken** | Slicer drew 1 line instead of 2 | Enable thin-wall detection / "Arachne" in the slicer; or raise `lip_wall` to 1.0 (costs 0.4 mm of battery room) |
| **Back edge flares out at the bottom** (elephant's foot) | First layer squished | Raise `edge_chamfer` to 0.6, or lower the bed temperature |
| **Corners lift off the bed** | Warping | Brim; clean bed; PETG on a textured or glue-sticked bed |

---

## Plugs

| Problem | Likely cause | Fix |
|---|---|---|
| **Plugs fall out** | Printer makes holes big | Raise `plug_interference` by 0.05, export `part = plugs` again |
| **Plugs won't go in** | Printer makes holes small | Lower `plug_interference` by 0.05 |
| **Plug won't sit flush** | Stringing in the recess | Clean the recess with a knife; or lower `plug_cap_t` a touch |

| Opening, close up | Plug fitted, close up | Section through a fitted plug |
|---|---|---|
| ![Tower opening and its recess](preview-plug-close-open.png) | ![Plug sitting flush in its recess](preview-plug-close-fitted.png) | ![Section: plug cap in the recess, ribbed shank in the bore](preview-plug-section.png) |

In the section (orange is the plug) the cap fills the recess flush with the back face and the
ribbed shank grips the bore. The screw head sits far above, under the seat.

---

## Finish

| Problem | Fix |
|---|---|
| **Ribs too strong or too subtle** | `grip_depth`: 0.2–0.5, or 0 for smooth walls |
| **Want a different texture** | `grip_style`: ribs, honeycomb, nubs, none |

---

## Still stuck

Open the `.scad`, press **F5**, and read the console. It prints **every clearance the model
worked out** — the gap at each end of the cell, the chamfer sizes, the hex key reach. The number
that's wrong is usually already on screen.
