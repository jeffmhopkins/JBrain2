# JBrain landing-page art — concept mockups

Five visual-style mockups for the public landing page. Open
[`index.html`](./index.html) for the gallery, or open any file directly.

Each mockup is a **single, self-contained HTML file** with **no dependencies**
(no Three.js, no build step) — a hand-rolled, pure-canvas **software-3D
renderer** in the low-poly-neon style of the reference
(`kc3efj.net/aquarium.html`).

## The concept

- A 3D **flock of boids** (Reynolds separation / alignment / cohesion) drifts
  through space with depth fog and motion trails.
- A lone **predator** hunts the flock; boids flee, the swarm swirls.
- One boid is "real": its **brain is a live feed-forward neural network**
  overlay. Its 6 input neurons are driven by that boid's actual per-frame state
  — density, speed, predator threat, and the separation/alignment/cohesion
  force magnitudes — so pulses cascade through the net as the boid reacts.
- **Click any boid** to retarget the neural map to that boid. A calm interior
  boid and a hunted one produce visibly different firing.

Controls: drag to orbit · scroll to zoom · click a boid · `H` toggles UI.

## The five styles

| # | Name | Feel |
|---|------|------|
| 01 | **Neon Reef** | Cyberpunk bioluminescence — cyan→magenta, bloom, light-streak trails. |
| 02 | **Ghost Ink** | Monochrome sumi-e — ink brush-trails on charcoal; predator is the only color. |
| 03 | **Aurora Drift** | Soft calm — twilight gradient, mint/teal haze, slow drift, stardust. |
| 04 | **Wireframe Genesis** | Holographic blueprint — wireframe boids, ground grid, live numeric neural trace. |
| 05 | **Synapse Storm** | Brain-forward neuron-art — the neural net is the hero, flock is the hazy mind behind it. |

## Status / next steps

These are **art prototypes to pick a direction** — not wired into the app yet.
Once a style is chosen, the planned product behavior is:

- The web interface shows **only this landing page** to the public. There is
  **no login screen** by default.
- Login is offered **only until the first successful login from the installed
  PWA**. After that the login affordance is gone.
- If the PWA loses its cookie/session, recovery is via a **console command**
  (a manual reset), not a re-exposed web login form.

That auth-gating is **server/app work, deliberately deferred** — the request
was to build the landing-page art first.
