# GrowBot → the creature's default face

Kind: Living (mock set) · Status: **Open**, three variants, awaiting the owner's pick · Last verified: 2026-11-10

The owner is not a fan of the face GrowBot draws today (`growbot/site/v3/next.html:drawFace`:
two 12 px rectangle eyes on a 96 x 160 pixel canvas, upscaled, over the mood sky) and asked for
alternatives, with the JPanels' robot and ostrich as a starting point. Three directions:

| | Variant | What it is |
|---|---|---|
| A | `a-ostrich.html` | The panels' ostrich (`firmware/main/face.c:draw_ostrich`, `../room-endpoint/ostrich-mock.py`) cropped to head and neck. The beak talks, the crest perks and droops. |
| B | `b-robot.html` | The panels' robot head (`face.c:draw_robot`) at phone size, with a crop of the chest. The smile opens to talk; the antenna lights while thinking, the chest light while the ear is open. |
| C | `c-lantern.html` | No head, like today, redrawn at full resolution: two big glowing eyes in the mood colour that carry emotion by shape, a pupil only when it has a job, and a voice line under them. |

Open any of the three directly in a browser. They are standalone and make no network request
beyond Google Fonts. `faces.js` holds the three drawings; `shared.js` holds everything they
share, lifted from today's face so the variants differ only in shapes: the seven emotions the
brain sets (`EMO`, `GLOW`), the mood sky's hue and stars, the blink, the 1.6 s glance at a touch,
the 8-step pupil orbit while a think is in flight, the searching squint while looking, the nap,
the dream veil, the shake that scatters the stars, and captions in the existing pixel font.
A and B also take the panels' body colours. The controls beside the phone are mock controls,
not design.

## What every variant keeps

- **Driven by the same state.** The brain still only sets `emotion`, and the mood, think, look,
  dream and pause states drive the face exactly as they do today. Nothing new crosses the
  prompt or the soul.
- **The mood sky stays.** Its hue is the only place the creature's inner weather shows.
- **A blink is a line, not a gap**, as on the panels: a face that vanishes for a frame reads as
  broken.
- **Speech moves something.** Today's face has no mouth, so nothing on it says *I am the one
  talking*. A's beak, B's mouth and C's voice line all move only while it speaks.
