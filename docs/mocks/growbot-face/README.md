# GrowBot → the creature's default face

Kind: Living (mock set) · Status: **A chosen** (owner, 2026-11-10); **D ember** added for Dave (owner, 2026-11-12) · Last verified: 2026-11-12

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

## Chosen: A, the ostrich

The owner picked A on 2026-11-10. Two rounds of feedback before that changed the necks: the
ostrich's long ringed neck with nothing under it read as too long and a solid neck lost the
panels' look, so it is now a short ringed neck on the bird's body; the robot gained a ringed
neck. The owner also asked that colours still change, which is the `coat` verb
(`docs/reference/DESIGN.md` "GrowBot — the creature's face"). B and C stay here as the rivals,
and stay reachable in the app with `?face=robot|lantern`.

## D: the ember, Dave's lens (owner, 2026-11-12)

The owner asked for a face for his adult persona, Dave, evocative of a calm, watchful AI lens
from 2001-era film: one glowing eye, a dark bezel, minimal, and an original design rather than
a replica of any prop. `d-ember.html` shows three lens directions live, with a state switcher,
a pointer the eye follows and a tilt slider:

| | Direction | How a mouthless lens talks |
|---|---|---|
| A | **Ember** — glass lens, lit core as pupil | The core swells and brightens with the voice; a ring of light leaves it per syllable; listening pulls rings inward |
| B | **Iris** — seven-blade aperture behind glass | The aperture flutters open with the voice and turns slowly while listening |
| C | **Dial** — small lens in a ring of 72 light ticks | The ring becomes a mirrored radial waveform of the voice; a sweep runs round it while listening |

The owner picked **A** and it is built as the `ember` face (`growbot/patches/growbot-face.js`,
spec in `docs/reference/DESIGN.md` "The ember — Dave's lens"). B and C stay here as the rivals.

