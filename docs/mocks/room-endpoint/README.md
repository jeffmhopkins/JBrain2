# Room endpoint — GUI round 1

Interactive mock: **`device.html`** (one file, four shapes, one state model).
Plan: `../../proposed/ROOM_ENDPOINT_PLAN.md` · Design system: `../../reference/DESIGN.md`

Per `DESIGN.md` "UI development process": a new surface gets **3–4 distinct variants**
before implementation, and there is no reuse exemption. These four differ in *what the panel
is for* and *what touch means* — not in colour.

## The finding that should decide this round

The plan was written before anyone worked out the panel's physical size.

> **1.8″ diagonal at 368×448 = 29.0 × 35.3 mm, 322 ppi.**

That is a **smartwatch panel, about a postage stamp**, and it invalidates three things the
plan cheerfully assumed:

| Plan said | Actually |
|---|---|
| "a 368×80 caption strip" | 6.3 mm tall — **one** short line, not a status bar |
| "the box's face in a room" | unreadable beyond arm's length; this is a **desk** object |
| 16×16 at 23 px/cell | the whole creature is **29 mm** — a thumbnail |

12 px type is 0.95 mm. Nothing on this panel should sit below ~28 px (2.2 mm), and anything
that matters wants 40 px+. **One thing at a time, large.** All four shapes obey that; they
disagree about which one thing.

**Tick "True physical size" in the mock before judging anything.** At 1:1 device pixels on a
desktop monitor the panel looks like a phone screen and every shape flatters itself.

## The four shapes

| | Shape | The panel is… | Touch is… | What it costs |
|---|---|---|---|---|
| **A** | **Matrix** | a 16×16 LED matrix, true black, hard cells, one caption line | the pet, and nothing else | the pet is the whole UI, so anything that isn't the pet has nowhere to go |
| **B** | **Porthole** | a window onto a creature drawn at full 368×448 — smooth, big-eyed, tracking your finger | drag to pet it; chrome appears only while touched | no persistent affordances — lovely, and undiscoverable by anyone but the owner |
| **C** | **Face** | one enormous state: the clock, the listening ring, one notification, or the pet | tap advances, swipe dismisses | the pet is a guest at 8 px/cell — if the pet is the point, this isn't it |
| **D** | **Room** | a 2D side-on echo of the Wall's 3D room; the pet lives in it | tap an object, the pet goes and does the shipped command | four props don't fit on 29 mm — the pet occludes its neighbour even at 6 px/cell |

A and B are "a creature you keep". C is "a thing the box talks through". D is "a place the pet
lives", and is the only one that reuses the Wall's vocabulary as its interface.

## Settled, and identical in all four

- **The state machine is the endpoint's, not the pet's** — idle / listening / thinking /
  speaking / notify / muted. It comes from the box. Run the same sequence in each shape
  (Wake word → Speak → Notify) before choosing; a shape that only looks good idle is not a
  shape.
- **Muted is a promise, not a UI state** (plan §5). Muted says so unmistakably and never stops
  saying so; listening says it just as loudly. No ambiguous middle, and no shape may trade the
  indicator's obviousness for prettiness.
- **Nothing firewalled renders here.** The notification carries a count and a category, never a
  body. "3 proposals" is fine; the sentence is not. This panel sits in a room.
- **The pet belongs to the box.** `jpet/` is server-authoritative and `PetBroadcaster` already
  fans state to the Wall and the phone — an endpoint is one more subscriber. These shapes
  render pet state; they never invent it. Forms match the Wall's: robot, dog, cat, dragon.

## What the mock also demonstrates

- **True physical size** toggle (CSS mm) — the whole point.
- **23 px cell grid** overlay, showing 368 ÷ 16 = 23 exactly, with the caption-strip line at 368.
- **AMOLED burn-in drift** — a static face on an OLED for months is a real hazard; firmware
  would drift the frame, and the mock shows what that looks like.
- **Long-press to mute** works in every shape, because the mic control must never depend on
  which screen you happen to be on.

## Open questions for the review

1. **Which shape** — and with it, what this device is *for*.
2. Does the postage-stamp size change the **deployment story**? A desk object at arm's length
   is a different product from the "room endpoint" the plan named, and two units might want to
   be two different shapes.
3. If **D**, does the room drop to three props or learn to pan?
4. If **A**, is a 16×16 pet on a 322 ppi panel a deliberate aesthetic or a waste of the screen?
   B exists to make that question concrete.
