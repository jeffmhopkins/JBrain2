# JBrain2 — JPet v2: play companion (positive, command-scripted, room-aware)

> **Status:** Proposed · **Last verified:** 2026-07-04 · **Waves:** W1◻️ W2◻️ W3◻️

A redesign of the shipped wall pet (`../archive/JPET_PLAN.md`) that keeps the
3D Tron room, the server-authoritative `pet_state`, the SSE fan-out, the phone
Control screen, and the on-box `:8800` wall — but **changes what the pet is
for**. v1 shipped a Tamagotchi: drives *decay*, the pet gets *hungry / neglected*,
and the kid's job is to stave off sadness. For a 3–4-year-old that is both the
wrong cognitive model and an affective liability. v2 pivots to a **positive,
command-and-response play companion**: the pet is always happy to play, the kids
*tell it to do things* ("dance", "run in circles then jump", "go hide", "pick up
the ball and put it in the corner"), and it does them — with sound. Nothing
decays; there is no fail state.

This doc is `Proposed` — nothing built. It supersedes the *interaction model* of
`../archive/JPET_PLAN.md` (which stays the shipped record of the v1 surfaces the
kids still use); when v2's waves land, its own status climbs the ladder and v1's
decay framing is retired in prose.

## Why (the research, three dossiers)

Three deep-research dossiers back this pivot (design research, to be filed under
`../archive/research/` if v2 is scheduled). The load-bearing, *verified* findings:

- **Decay/neglect is wrong for this age.** 3–4-year-olds grasp simple
  cause-and-effect ("I did something → something happened") but struggle to map
  input direction to a screen target; and their grief at a pet's decline can
  exceed an adult's in intensity and duration. "Your pet is sad / dying" stakes
  are cognitively mismatched *and* affectively risky. What works instead:
  command→reaction, call-and-response, mimicry, hide-and-seek, dancing, silly
  sounds, naming, turn-taking — always rewarded with an immediate positive
  animation. (Neko Atsume is the reference: a no-fail, no-command, place-things-
  and-watch loop *explicitly designed to be enjoyable for children*; The Sims
  deliberately *caps* pet autonomy so it never autopilots the player out of a
  role.)
- **LLM → motor is a bounded-vocabulary select, not free generation.** The
  established pattern (ProgPrompt, LLM-Planner, SayCan; VTuber/Live2D motion-tag
  systems) is: give the model a listing of the available named primitives +
  in-scene objects + a few example scripts, and have it emit an **ordered list of
  parameterized steps drawn only from that fixed set**. Bound it at decode time
  with an enum-restricted, `strict` JSON schema (and belt-and-braces: a host-side
  allow-list), so it can *only* pick known-good primitives and known objects —
  **never arbitrary code**. A tiny toy needs no behaviour tree / PDDL / GOAP: a
  **flat list with a hard length cap and a required terminating idle/sit/sleep
  step** is always-terminating and sufficient. "Grounding" reduces to a cheap
  affordance check ("is the ball actually in the room?") that drops or repairs
  impossible steps.
- **Phone control for a 3–4-year-old:** large (≥2 cm, ~4× adult), well-separated
  targets; few choices; single simple taps (no timed / two-handed gestures);
  register on **touch-down** with immediate audiovisual feedback; parent-gate or
  confirm anything consequential; no dead-ends, no destructive actions.
- **Sound:** playful per-action babble/beeps over real speech, capped volume to
  avoid startle, and browser audio unlocked on a user gesture (autoplay policy).
  The gesture-unlock + sink keep-alive on the `:8800` wall already ships (the
  pet now speaks its bubble aloud via piper).

## The v2 interaction model (replaces the drives)

The four drive numbers (`food/energy/fun/love`) **stay in the DB** — but their
*meaning and framing* change and they never create stakes:

- They are **positive mood inputs**, not a countdown. Playing with the pet
  *raises* them; they **do not decay toward sad/hungry/neglected**. The wall/phone
  never says the pet is starving or unloved.
- The HUD reframes them as happy meters ("full of beans", "so much fun") or drops
  the bars entirely in favour of a single bright mood. (Decision deferred to W1
  design; leaning: keep the bars but recolour/relabel as always-positive fill.)
- The pet is **always willing to play**. Idle behaviour is Neko/Sims-capped
  ambient life (wander to the ball, curl up in bed, look at the child) that never
  competes with a command — a kid command always wins and interrupts idle.

Everything the kid does is **command → immediate delightful reaction**.

## The action-script schema (what the LLM emits)

The talk brain's reply grows from `{speech, emotion, action}` to a **short ordered
script**:

```jsonc
{
  "speech": "okay! watch me!",
  "emotion": "excited",                       // happy|excited|curious|sleepy|silly|scared
  "script": [                                  // ordered; maxItems 6; last step must terminate
    { "action": "chase",  "target": "ball" },
    { "action": "spin",   "duration_ms": 1200 },
    { "action": "dance",  "duration_ms": 2000, "emotion": "silly" },
    { "action": "sit" }                        // terminating step (idle|sit|sleep)
  ]
}
```

Each step: `action` (enum, required) + optional `target` (object enum),
`destination` (location enum), `duration_ms` (200–3000), `emotion` (enum).

**Starter primitives (~18):**
`go_to · pick_up · put_down · carry_to · dance · spin · jump · wave · hide ·
chase · wiggle · sit · sleep · wake · nod · beep · look_at · come_here`

**Starter objects:** `ball · bed · toy_box · food_bowl · ball_pit · light_switch`
**Starter locations:** `corner_ne · corner_nw · corner_se · corner_sw · center ·
near_child`

**Bounding & safety (host-side, belt-and-braces over the schema enums):**

- Emit via the LLM adapter with the enum-constrained JSON schema (the same
  `router.complete(json_schema=…)` path the v1 brain uses; `_clean()` already
  sanitizes off-enum output — extended to the new vocab).
- **Length cap** (`maxItems 6`) + **required terminating step** (idle/sit/sleep)
  + a **host-side max-step failsafe** → every script is short and always-terminating.
- **Affordance check:** drop or repair any step whose `target` isn't currently in
  the scene (allow-list), so a hallucinated object can't wedge the runner.
- No primitive is destructive; there is nothing to break or lose.

## The freeform-request → script pipeline

1. Kid speaks or the parent types (existing STT + talk box on the Control screen).
2. `pet.turn` runs through the LLM adapter with the schema above; the system
   prompt lists the primitives, the in-scene objects, and 2–3 example scripts
   (ProgPrompt-style), plus the always-positive persona.
3. `_clean()` validates: enum allow-list, length cap, terminal step, affordance
   drop. Result persisted as the pet's active `script` + step cursor.
4. The **script runner** (in the drives tick loop — arithmetic, second seat)
   advances one step at a time, driving `pos/target/facing/action` and firing
   per-step sound cues, broadcasting each transition over the existing SSE.
5. **Latency:** because it's an ordered list, execute step 1 optimistically the
   moment it decodes; a small fast model + tiny vocab keeps it snappy.

## Phone Control buttons (the kid surface)

Replace the v1 care buttons (feed/play/pet/poke) with **big, few, non-destructive
play buttons**, each a one-tap canned script with touch-down sound + animation:

`💃 Dance · ⚽ Chase the ball · 🙈 Hide & seek · ⭐ Jump! · 👋 Wave hi ·
😴 Sleep / ☀️ Wake · 🔊 Silly sound` — plus the **push-to-talk mic** for freeform
requests. The room-map "move" control stays as a *parent* affordance (kids use the
named buttons + voice, not directional targeting).

## Room props

Static named props with known positions the pet pathfinds to (smart-object
pattern: the prop carries its interaction). v1 ships the empty grid room; v2 adds,
ranked by delight-per-effort:

1. **Ball** — chase / pick up / carry / put down (the marquee toy). Cheap wireframe
   sphere; carry = parent to the robot's "hands".
2. **Bed** — go to / sleep / wake. Cheap box; pet curls (the `sit` pose exists).
3. **Toy box** — rummage (canned dip-and-pop-out). Cheap box.
4. **Food bowl** — the old `eat`, reframed as a happy snack, not hunger relief.
5. **Light switch** — day/night toggle (ambient delight; ties to the deferred
   day/night lighting in `ROADMAP.md`).
6. *(later)* ball pit, drum/xylophone (bang → sound), bubbles to pop.

## Sound

- Pet **speech** → piper `/tts` on the `:8800` wall (**shipped**).
- **Per-action cues** → short WebAudio babble/beeps generated on the wall (no
  assets), one per primitive (a rising blip for `jump`, a wobble for `wiggle`, a
  little fanfare for `dance`), volume-capped to avoid startle, behind the existing
  gesture-unlock.
- Optional playful non-word "Furby-talk" babble layer under speech (deferred).

## Waves

| Wave | Scope | Lands |
|---|---|---|
| **W1 — Positive framing + action scripts (objectless) + kid buttons + sounds** | Persona pivot (no decay/neglect); `script` array with the objectless primitives (dance/spin/jump/wave/wiggle/nod/hide/come_here/sit/sleep/wake/beep); script runner in the tick; extended `_clean()`; big kid play-buttons + touch-down feedback on Control; per-action WebAudio cues on the wall; HUD reframed positive. | migration for `script`/cursor columns; backend brain+runner+tests; frontend Control; `pet.html` primitives+cues |
| **W2 — Room objects + object-targeted actions + carry** | Props (ball/bed/toy_box/food_bowl) with positions + state; `go_to/chase/pick_up/carry_to/put_down/look_at` targeting; pathfind-to-object; affordance grounding; `pet.html` renders + animates props and the carried ball. | migration for room-object state; backend targeting+pathfind+tests; wall prop rendering |
| **W3 — Freeform polish + autonomy + latency** | ProgPrompt-style prompt with in-scene listing + examples; optimistic first-step playback; Neko/Sims-capped ambient idle that uses the room; startle-safe volume tuning; residual props (drum/bubbles/ball pit). | brain prompt; runner streaming; idle behaviour; extra props |

W1 is independently shippable and delivers the user's headline asks ("dance",
"run in circles then jump", working sounds, fun buttons). Object carry ("pick up
the ball, put it in the corner") is W2.

## Non-negotiables (unchanged from `CLAUDE.md`)

LLM only via the adapter; DB only on RLS-scoped sessions; the kid principal never
sees health/finance/location; every new table/column ships an RLS isolation test;
tests land in the same PR (80% / security 100%); the `:8800` wall stays DB-free,
unauthenticated, LAN-only, `/internal` never off-box; drives + script runner stay
arithmetic and **off the job queue** (always second seat). Docs travel with the
code.
