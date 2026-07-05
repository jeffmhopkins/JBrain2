# JBrain2 — JPet v3: a pet that's genuinely alive (autonomy engine on the wall)

> **Status:** In progress · **Last verified:** 2026-07-05 · **Waves:** W1✅ W2✅ W3◻️

**W1 shipped** (migration 0126): the engine reboot — the wall runs the pet's continuous
autonomous life (constrained-randomness behaviour selection, damped-spring fluid motion,
always-on idle micro-motion), the drive meters are ripped out, the robot + props render in
solid wireframe, and the room is 2× with a reframed camera.

**W2 shipped** (wall-only): the living, interactive world — **ball physics** (rolls/bounces/
settles; the pet kicks + chases; **mouse click** flicks it or sends the pet); the
**block-builder** (the pet gathers small coloured solid bricks and stacks them into big
varied **statues** — pyramid/castle/tower/arch/staircase/rocket — a new shape each rebuild;
a rolling ball knocks the statue down and it rebuilds); **detailed furniture** (bed/toy box/
food bowl) + a **TV** (flickering screen the pet watches) + a **window** (a day/night sky it
gazes out of); **circadian** day/night from the real clock (dim + moon/stars + sleepy at
night); and a **vacuum** tool the pet uses to tidy loose blocks. W3 (reliable talk + activities
+ colour) follows.

The v3 leap: JPet stops being a server-driven puppet and becomes a **living little
creature** that potters around its room *all the time*, on its own, fluidly — and does
what the kids say, reliably. It supersedes v2's *server-authoritative discrete scripts*
(`../archive/JPET_V2_PLAN.md`) — that model is why the pet is idle-then-steppy and why
talking sometimes does nothing. Backed by a deep-research dossier (105 agents) on
autonomous-creature design, whose load-bearing findings drive every decision below.

## The core architecture shift: the WALL runs the pet's brain

Today the server emits one discrete action every ~30s and the wall plays it step-by-step.
A 30s server tick can *never* look "constantly, fluidly alive." So v3 moves the simulation
to where the frames are:

- **The `:8800` wall is the authoritative real-time simulation** — the pet's brain, the
  physics, the animation, the mouse interaction, the clock. It runs a continuous 60fps
  loop. This is the only place that can produce fluid, non-stop life.
- **The server (`:8000`) holds durable state only** — the pet's name, its **color**, its
  **memory**, and a small **command inbox** (the latest phone/talk command + a nonce so the
  wall runs each exactly once). The drives/meters and the autonomous tick are **deleted**.
- **The phone Control** sends commands to the server; the wall polls the server (~1s, as it
  already does) for durable state + any pending command and applies it to its live sim. A
  kid tap reaches the wall within ~1s — instant enough for a pet.

This keeps every existing safety boundary: the wall stays DB-free / unauthenticated /
LAN-only, `/internal` never routes off-box, all LLM via the adapter, DB on RLS sessions.

## Research-backed pillars

1. **No meters — read mood from behaviour** *(validated: PF.Magic Petz exposed NO bar
   graphs, forcing mood to be read from behaviour, which "preserves the illusion of a
   relationship with something alive").* Rip out food/energy/fun/love entirely. The pet has
   an internal **mood** that drifts (curious/playful/sleepy/excited), shown only through what
   it does and how it looks — never a number.

2. **Constant, non-repetitive autonomy via a continuously re-evaluated behaviour model.**
   The cure for "steppy" is to **re-score all possible behaviours every frame** and let the
   pet abandon/chain actions without committing to a fixed sequence (utility AI: normalize
   state to 0–1, pick the top-scoring action; Creatures' winner-takes-all is the same idea).
   Variety comes from **"constrained randomness"** — per-pet biases (favourite toys, quirks)
   plus a randomness factor — and many small mechanisms running in **parallel** (a current
   goal, a wander drift, an attention target, plus always-on blink/breathe). Recency
   weighting stops repeats. Net effect: the pet is *always doing something*, and the next
   thing flows out of the last with no dead air.

3. **Fluid, non-robotic motion, client-side.** All position/rotation transitions use
   **damped-spring (exponential-decay) interpolation** — nothing snaps. Locomotion has
   **separately tuned acceleration/deceleration** (friction ≈ 4/T) and **turn rate scaled by
   speed** (it leans into turns, slows before it stops). Under *everything*, always-on
   **idle micro-motion**: breathing bob, blink, weight shift, head/eye look-at, antenna
   sway — so the pet is never perfectly still. Actions **blend** into each other via a short
   crossfade, never a hard cut.

4. **A living, interactive room from "smart objects".** Each object **embeds its own
   interaction** (a "behaviour object"): the ball knows how to be chased/kicked, the blocks
   know how to be stacked, the synth knows how to be played. Objects also **live on their
   own** (the ball rolls and settles, the TV flickers, the window sky drifts). **Mouse
   interaction** follows the Neko lineage: click a point/object → the pet *notices* (a cheap
   movement-threshold **attention trigger** + a dedicated **"surprise" reaction** so a click
   *always* yields a visible response) → goes and plays with it.

5. **Talk that always does something — hybrid confidence router.** Fixes the v2 bug (the
   `say` path 500s when grok isn't configured). New pipeline: a **fast local intent
   classifier** (keyword/rule) runs first — simple, high-confidence commands ("dance",
   "blue", "sleep", "kick the ball") execute **immediately, no LLM**. Only ambiguous/creative
   input goes to the LLM, and the LLM's output is **grounded to the fixed action pool**
   (nearest valid action) with a **safe default** when it can't match — so "says something
   but does nothing" is impossible. LLM errors degrade to the classifier + a friendly babble,
   never a 500. Optimistic local playback keeps it snappy.

## The world (all 19 owner threads)

**Room — the "run", 2× bigger + zoned.** Double the room and lay in **geography**: a
**play corner**, a **build zone** (block statues), a **cozy nook** (bed + TV + window), a
**toy area** (toy box, jump rope, synth). Re-frame the camera so it still reads at the
larger size. Distinct zones give the pet real places to roam between.

**Look — solid neon wireframe + detailed furniture.** Keep the glow, kill the X-ray:
render dark, mostly-opaque **occluding faces** behind the neon edges (needs a triangle-fill
pass; draw fill then edges). Rebuild the furniture from **multiple wireframe parts** so each
piece has character — bed (frame/legs/headboard/pillow), toy box (hinged lid), TV (stand +
antenna), a real food bowl, distinct blocks, a framed window, the synth.

**Items & activities:**
- **Ball (physics)** — velocity, rolls, bounces off walls, friction, settles. The pet
  **kicks** it and chases; click/flick it on the wall to send it rolling (pet gives chase).
- **Blocks + build loop** — the pet **constantly sorts and stacks** glowing blocks into
  evolving **statues**, occasionally knocks them down and rebuilds *differently* (recency
  weighting → never the same). Marquee ambient activity.
- **Ball → statue collapse (emergent physics)** — a rolling/kicked ball hits the statue and
  the blocks **scatter** (impulse + friction, block-vs-ball/block); the pet then **rebuilds
  or vacuums up** (varied cleanup).
- **Vacuum (carry + USE tool)** — a new verb: pick up → *use*. The pet hoovers up scattered
  blocks/dust with a whir; establishes a reusable tool pattern.
- **TV** — a set whose screen flickers its own content; the pet wanders over and **watches**
  (cozy passive beat); clickable to send it watching.
- **Window to outside** — framed animated view tracking the **real clock**: sun/clouds by
  day, moon/stars at night; the pet **gazes out** (esp. at dusk).
- **Synth/piano (real audio)** — the pet plays melodies (keys light in sync, real WebAudio
  notes); **kids can click the keys** and the pet dances along.
- **Jump rope** — swings a rope in a rhythmic arc and bounces in time (comedic trip-and-
  recover); on-command + self-directed when peppy.
- **Light switch** — already exists; made clickable + the pet flips it autonomously.

**Sense of time (circadian).** The wall reads the **real local clock**: lively by day, gets
sleepy at dusk (yawns, slows, heads to the nook, gazes out the window, naps more), mostly
sleeps overnight with the room auto-dimmed to a soft night glow, stretches awake at morning.
Biases behaviour selection by time of day. The manual light switch overrides. (Delivers the
day/night lighting deferred from v1.)

**Change colour on command.** "Turn red" / "go blue" / "rainbow!" by voice, a phone palette,
or a wall click → the wireframe + glow recolours instantly; sticks until changed (a `color`
on durable state). Prime cause-and-effect delight.

## Borrowed patterns (prior-art hybrid, keeping our robot)

- **VPet-Simulator** (Apache-2.0) → the animation / state / behaviour separation and the
  "always animated, mouse-pettable" liveliness.
- **AI-Tamago** (MIT) → an autonomous LLM inner-state loop that periodically nudges mood/
  intent (self-directed, not just reactive); memory continuity (we have `pet_memory`).
- **Open-LLM-VTuber** → the small keyword→motion vocabulary + the keyword fallback.

## Waves (phased — this is a big build; ship the feel first)

| Wave | Scope | Delivers |
|---|---|---|
| **W1 — Engine reboot** | Move the sim to the wall: continuous utility-AI autonomy (constrained randomness, continuous re-eval, parallel micro-behaviours); damped-spring fluid motion + always-on idle layer; **rip out meters** (migration drops the drive columns; server → durable state + command inbox; delete the autonomous tick); **solid-wireframe** rendering; **2× zoned room**. | Fixes infrequent/steppy, meters-gone, see-through, bigger room — the core "alive" feel. |
| **W2 — Living world** | Smart/behaviour objects; **mouse click-to-play** + attention/surprise; self-moving items; **ball physics** (kick/roll/bounce); **blocks + build loop + statue collapse** physics; **detailed furniture**; **vacuum**; **TV**; **window**; **circadian** day/night. | The physics playground + the room that lives on its own. |
| **W3 — Play & expression** | **Hybrid talk→action router** + keyword fallback (fixes the talk bug); activities (**jump rope**, **synth** with playable keys, watch TV); **colour change**; phone Control rebuild (no meters, activity buttons, colour palette, push-to-talk). | Reliable talk + the expressive/creative layer. |

## Non-negotiables (unchanged from `CLAUDE.md`)

LLM only via the adapter; DB on RLS-scoped sessions; the kid principal never sees
health/finance/location; every new table/column ships an RLS isolation test; tests land in
the same PR (80% / security 100%); the `:8800` wall stays DB-free, unauthenticated,
LAN-only, `/internal` never off-box; the pet never blocks the box's real processing (the
wall's sim is its own client loop; the server stays a thin durable-state layer). Docs travel
with the code.
