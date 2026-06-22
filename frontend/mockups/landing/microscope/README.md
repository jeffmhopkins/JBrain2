# JBrain landing art — microscope amoeba (round 3)

A hard pivot from the boid concept. Open [`index.html`](./index.html) for the
gallery, or any file directly. Each is a **single self-contained HTML file**,
vanilla canvas 2D, **no dependencies**.

## The concept

A single **amoeba** crawls a slide and **engulfs smaller cells**, seen **through
a microscope**. A tiny **5-node neural "brain"** sits in the corner: its inputs
are driven by the amoeba's real state (food proximity, hunger), so it's not
decoration — and when you **tap a neuron** it fires, ripples forward, and
visibly acts on the amoeba (reach / engulf). **Tap the slide** to drop food and
watch it hunt.

## Built from research + a red-team pass

Two agents ran first: one researched amoeba/blob rendering and microscopy
aesthetics; one red-teamed the concept against the user's prior "too busy"
rejections. Their **non-negotiable calm rules** are baked into every mockup:

- ≤ 4 food cells; amoeba near-circular at rest, ≤ 2 pseudopods; slow motion
- 5-node brain only (no hairball); tap-to-fire; no text/labels in the scene
- **no pan/zoom** (fixed scene — removes gesture conflict)
- DPR capped at 2; no CSS blur; all microscope chrome (vignette, dust) is
  **static and cached** to an offscreen canvas; `dt` clamped on tab restore
- amoeba always has a nucleus (or it reads as a bubble)

Rendering: deformable Catmull-Rom membrane (12 pts, summed-sine wobble),
clip-to-path layered interior (gel + granules + nucleus + food vacuoles),
source-over cached glow sprites (no `lighter`/blur, Safari-safe).

## The five modalities

| # | Name | Look |
|---|------|------|
| 01 | **Dark-Field Nocturne** | Glowing teal membrane on ink-black — the hero. |
| 02 | **Bright-Field Botanica** | Stained sage body on warm cream — the calmest, no neon. |
| 03 | **Phase-Contrast Study** | Grayscale lab image with the signature bright halo. |
| 04 | **GFP Fluorescence** | Multi-channel on black — green body, DAPI-blue nucleus, red food. |
| 05 | **DIC / Nomarski Relief** | Embossed pseudo-3D relief on neutral gray — sculptural. |

## Round 4 — `06-darkfield-advanced.html` (the chosen look, deepened)

The Dark-Field aesthetic was picked. This build upgrades the simulation from
two more research passes (`docs/AMOEBA_BRAIN_BRIEF.md` + an amoeba-locomotion
brief):

- **Real amoeboid locomotion** — 4 candidate pseudopod lobes that score on the
  food gradient and **compete via a softmax**; one **wins and extends**, the
  others **retract**. Body **flows into** the winning lobe instead of sliding.
- **Fountain-flow streaming** — live granules stream toward the leading tip and
  recycle at the rear; the nucleus is carried and lags.
- **Area conservation + neighbour tension** — extending a finger **deflates**
  the rear; shape changes propagate (Laplacian), so it morphs like a cell.
- **Cup phagocytosis** — flanking lobes wrap prey into a food vacuole;
  **contractile vacuole** fills and expels as an idle life-sign.
- **17-node controller brain** (8 inputs → 4 hidden → 5 outputs) in a calm
  **full-width bottom dock**: 4 directional chemoreceptors + hunger/energy/
  vacuole/edge → dirX/dirY/arousal/visceral → driveX/driveY/commit/engulf/expel.
  ~14 hand-picked **curved** edges (not a 52-edge hairball), cool→warm bands,
  a single faint tendril linking body↔brain. The net genuinely drives the
  amoeba, and **tapping a neuron** fires it back into the sim.

Verified headless (600 frames, no NaNs; competition + feeding confirmed).

Earlier rounds (boid flock, wireframe) live in the sibling folders.
