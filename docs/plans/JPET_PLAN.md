# JBrain2 — JPet: the wall pet (a robot avatar for the box)

> **Status:** In progress · **Last verified:** 2026-07-04 · **Waves:** W0✅ W1✅ W2✅ W3✅ W4✅ W5◻️ W6◻️

A **wall pet** for the family: a display shows a window into a 3D Tron/synthwave
room, and inside it a wireframe robot — an LLM-driven avatar that walks around,
gets *hungry*, *sleepy*, *bored*, and *moody*, and can be talked to. It has **two
surfaces**: the **Wall** (a full-screen 3D room on a mounted display) and a
**phone Control screen in the existing PWA**, so the kids can feed it, send it
around, and tell it to do things **from a phone** — with both surfaces kept in
sync in real time by the server. The pet is a **companion surface on top of the
existing box**, not a new brain: it runs beside JBrain and always **takes second
seat to the app's real processing**.

This is an `In progress` build plan under **Phase 7 (outer ring — family & devices)**:
the waves below are built in order. **W0–W4 have landed:** the `pet_state` table +
RLS firewall + drive math + tick; the `/api/pet` surface (`GET /pet`, `POST
/pet/command`, `GET /pet/stream` SSE fan-out); the `WallScreen` (a self-contained
WebGL room); the `ControlScreen` (the phone remote); and the **talk brain** — a
`pet.turn` LLM route (adapter, structured `{speech, emotion, action}`, safe kids'
persona) reached by a `say` command, with a talk box on the phone and a speech
bubble on the Wall. Both surfaces render the same server-authoritative pet and stay
in sync. **W5 (memory + autonomous life) is next.**
Every wave satisfies the `CLAUDE.md` non-negotiables.

**Chosen aesthetic + interaction (signed off):** the interactive 3D mockup
`../mocks/jpet/06-room-3d.html` — a real WebGL perspective room with the pet as a
wireframe robot that walks the floor, turns to face its heading, and reacts
(click-floor-to-walk, click-to-poke, drag-to-orbit), synthwave palette. The five
flat 2D sketches (`../mocks/jpet/index.html`, 01–05) are kept only as palette
references. The 3D mock is the visual + behavioural target for the **Wall** (§6).

## 0. The one design decision that de-risks everything: no training, no net

**We do not train a neural net, and we do not need one.** A virtual pet is two
independent things, and neither is a learning problem:

1. **The drives** (hunger, energy, boredom, mood) are *numbers on a timer* — the
   classic Sims "needs/motives" model. Hunger rises on a clock and drops when
   fed; energy falls awake, recovers asleep; mood is a pure function of the rest.
   This is arithmetic, not ML.
2. **The personality and speech** are an **LLM** — the one we already have behind
   the adapter. The model is never *trained* on the pet; it is *told* the pet's
   current state in its prompt ("You are Bolt. You're quite hungry (12/100) and a
   bit sleepy. A child said: …") and answers in character, returning structured
   `{ speech, emotion, action, move_target }`.

The one historically-famous pet that *did* ship a real neural net — *Creatures*
(1996) — is a footnote; nobody builds it that way now because an LLM gives richer
behaviour with zero training data. So the whole feature is a **server-authoritative
state row + a thin LLM route + two thin frontends**, all on substrate we already
have.

### Prior work we lean on (patterns, not dependencies)

- **"Generative Agents: Interactive Simulacra of Human Behavior"** (Park et al.,
  2023) — the canonical LLM-agent-with-memory loop (*memory stream → reflection →
  planning*). JBrain already has RAG + a memory system, so "it remembers you fed
  it yesterday" comes almost free (W5).
- **The Sims motive engine** — the drive-decay math and idle action-selection
  (when bored, pick the action that most relieves the worst need).
- **Tamagotchi** — the care-loop stakes (neglect → sad/sick) that make kids
  attach, and the **remote-care** model (you carry the control in your pocket).
- **Live2D / VTuber avatars** — an LLM face that talks with personality + TTS.
- **Three.js + `UnrealBloomPass`** — the render path for the Tron look (W2); the
  3D mock already proves the look in raw WebGL.

## 1. Two surfaces, one authoritative pet (the architecture)

The pet's truth lives **on the server**, in Postgres. Both surfaces are *views*
of that truth; neither owns it. This is what keeps the Wall and the phone in sync
and is the load-bearing decision of the whole feature.

```
   Phone Control (PWA)                    Wall (3D room display)
   status • care buttons                  renders authoritative state
   "go here" • talk box                   local poke / click-to-walk
        │  POST /pet/command                       │  POST /pet/command
        └───────────────┬──────────────────────────┘
                        ▼
              FastAPI  (api/pet.py)
        apply command → mutate app.pet_state
        (+ optional pet.turn LLM) → append event
                        │
                        ▼  GET /pet/stream  (SSE fanout)
        ┌───────────────┴──────────────────────────┐
        ▼                                           ▼
   Phone re-renders                          Wall speaks + animates
```

- **Authority split.** The server holds **discrete intent state**: drive values,
  `mood`, `asleep`, `pos`/`target` on the floor, the current `action`, and the
  current `speech` utterance + `emotion`. **Animation is client-side** — each
  surface interpolates the walk, bob, blink, and bloom locally between server
  updates. So the server publishes small deltas on change (not 60 fps), and both
  clients stay coherent because they render the *same* authoritative intent.
- **Commands** (`POST /pet/command`, body `{action, payload}`) are the only way to
  mutate the pet from a client: `feed`, `play`, `pet`, `sleep`, `move {x,z}`,
  `poke`, `say {text}`. The endpoint authorizes the caller's principal (§3),
  applies drive deltas / sets a target / triggers a `pet.turn`, then broadcasts.
- **Real-time** is **SSE, not WebSockets** — matching the repo's existing choice
  (`api/agent.py`, `api/live.py` stream over `text/event-stream`; WS is reserved
  for the jcode terminal). `GET /pet/stream` is a detached, reconnect/replayable
  fanout exactly like the agent chat stream. Client→server stays plain `POST`.
- **Multiple viewers are free.** Because the Wall is just a renderer of server
  state, a second wall, the phone, and a tablet can all watch at once and agree.

## 2. What already exists (reuse, don't rebuild) — cited

- **LLM adapter + task-profile router** (`llm/router.py`): every call runs under a
  named task in `TASK_DEFAULTS` (task → `provider:model`), individually routable
  via `JBRAIN_LLM_TASKS`, with a per-task reasoning bucket
  (`TASK_REASONING_BUCKET`). The pet adds **`pet.turn`** (respond to a child) and
  **`pet.thought`** (idle daydream) — never a provider SDK (non-negotiable #1).
  Structured output via `json_schema` gives `{speech, emotion, action, move_target}`
  with no parsing.
- **On-box local model** (`llm/local_gateway.py`, `llama_swap_config.py`,
  `residency.py`): the pet routes to the local gateway by default so it never
  spends API budget and never contends with the paid path — the mechanism that
  makes "second seat" true by construction (§5).
- **SSE fanout + detached runs** (`api/agent.py`, `api/live.py`): the
  `text/event-stream` transport with reconnect/replay that `GET /pet/stream`
  mirrors — no new realtime infra, no WebSockets.
- **Scheduler tick loops** (`tasks/scheduler.py`, `TICK_INTERVAL_SECONDS`): the
  lightweight asyncio driver the drives tick copies — deliberately **off the
  single-threaded job queue** (§5).
- **RLS + isolation-test pattern** (`db/session.py`, `test_lists_rls.py`): every
  new pet table ships `ENABLE/FORCE RLS` + an isolation test (non-negotiable #3);
  this is also the kids-safety firewall (§3).
- **Storage abstraction** (`FsBlobStore`): any pet asset by sha256, never raw
  paths (non-negotiable #2).
- **Frontend** (React 18 + Vite + TS PWA, `frontend/src/screens/`, `Launcher`,
  `api/client.ts`, the mock-first `dev:mock` workflow): the **Wall** and the
  **Phone Control** screen are two new screens + launcher tiles; LLM routing gets
  a **new "JPet" card in `LLMSettingsScreen`** (§7). The PWA is already mobile-
  first with an offline outbox — the phone control surface fits its existing shell.

## 3. Kids-safety is a firewall problem (and we already have the firewall)

The pet is fed "information from the environment," the audience is children, and
now **anyone holding the phone can send it commands** — so the firewall matters
twice: what the pet can *see*, and who can *drive* it.

- **What it sees.** The pet runs under a **dedicated low-privilege principal**
  scoped to a deliberately narrow, safe domain set (a general/family domain only).
  Its environment feed is a **curated digest** ("it's evening; the house has been
  quiet") assembled *inside* that scoped session, so out-of-scope facts are
  invisible at the query layer — **never** health/finance/location. `pet.turn` /
  `pet.thought` prompts are built from that scoped session only. An isolation test
  asserts the pet principal cannot read a firewalled domain (security path, 100%,
  non-negotiable #5).
- **Who drives it.** `POST /pet/command` authorizes the caller as a **kid/family
  principal** (a device session on the phone), scoped to the pet's domain. A child
  principal can send care + talk commands but has no reach into owner data; the
  command handler never echoes anything outside the pet's scope back to the Wall.
- **What it says.** `pet.turn` output is constrained to the pet persona +
  scoped feed; it cannot surface a firewalled fact because it never receives one.

## 4. The drives model (new table, pure arithmetic) — **built (W0)**

**`app.pet_state`** (migration 0123) — one row per pet, one per `(principal_id,
domain_code)`. Columns: `id`, `principal_id`, `domain_code`, `name`, `food` /
`energy` / `fun` / `love` (0–100 satisfaction floats, higher = better — the names
the Control screen shows), `mood` + `emotion` (derived, materialized for cheap
reads), `speech` (the current utterance), `asleep` (bool), `pos_x` / `pos_z` /
`target_x` / `target_z` / `facing` (the floor position + heading the Wall renders),
`action` (CHECK-enum: idle/walk/eat/play/sleep), `last_tick_at`, `created_at`,
`updated_at`. Owner-only + domain-firewalled RLS, isolation-tested. The drive math
(decay, sleep energy recovery, mood thresholds) is a pure, unit-tested module
(`jpet/service.py`); the repo/tick just apply it.

The **tick** (owner-configurable cadence, ~5–30 s for movement responsiveness)
advances each drive by `elapsed × rate`, clamps 0–100, recomputes `mood`, flips
`asleep` on the day/night schedule or when `energy` bottoms out, and advances
autonomous **wander** targets. **No LLM in the tick** — a few multiplies + one
UPDATE, effectively free, never queued. On any change it publishes a delta to
`/pet/stream`. Commands (feed/play/pet/move) apply small deltas / set `target` on
the same row and publish immediately.

Idle **action-selection** (W5) is a tiny rule, not a model: with no child
interacting, pick the action that most relieves the worst drive (walk to a food
bowl when hungry, dim and sleep at night), and only *occasionally* spend a
`pet.thought` LLM call to narrate it.

## 5. "Second seat to the app's real processing" — the honest design

The job queue (`queue.py` / `worker.py`) is **single-threaded, FIFO by
`run_after`, with no priority column** — multi-worker/priority is explicitly
deferred (`archive/WORKFLOW_ENGINE_PLAN.md` §7). So a "low-priority background
lane" is not expressible on the queue today. **The pet sidesteps the whole problem
instead of adding a scheduler tier:**

1. **Drives tick + command handling = arithmetic in the web process** → never
   enqueued, ~free.
2. **Pet LLM calls default to the on-box local model** → they never touch the paid
   API and never compete with real JBrain jobs on the shared worker.
3. **The idle `pet.thought` call is skippable** → gated on local-model
   availability; if the box is busy, drop the daydream (residency check /
   `defer()`-style yield). Kids never notice a missed thought; real work is never
   delayed. (Care commands still work — they need no LLM.)

True on-queue deprioritization (a priority term in `claim()`'s `ORDER BY`, or a
separate lane) is net-new and **out of scope for JPet**.

## 6. The Wall — 3D Tron room (frontend) — **built (W2)**

The full-screen **Wall** screen (`frontend/src/screens/WallScreen.tsx`), launched
from a `Launcher` tile, for a wall-mounted display / old tablet in kiosk mode. It
**renders authoritative state from `/api/pet/stream`** (the `api.petStream` SSE
generator) and interpolates the animation locally; it does not compute the pet,
only draws it. Local input (click-floor-to-walk, click-to-poke) and the care
buttons emit `POST /api/pet/command`, through the same authority path the phone
uses.

- **Renderer: a self-contained WebGL engine** in `screens/petScene.ts` — the
  verified `06-room-3d.html` engine ported to TypeScript (perspective room, wireframe
  robot, walk interpolation toward the server `target`, turn-to-face, bob, blink,
  `emotion → pose`, a 2D-canvas bloom pass). **No external 3D dependency** —
  chosen over Three.js/`UnrealBloomPass` to reuse the proven engine, keep the
  dep footprint lean (so `package.json`/`dev-setup.sh` are untouched), and stay
  headless-verifiable; a Three.js swap remains an optional future polish.
- The scene is isolated behind the `petScene` module so `WallScreen` (state +
  subscription + commands) unit-tests under jsdom by `vi.mock`-ing it — the same
  convention as `leafletMap`.
- **Visual identity:** the Wall is a **deliberately committed neon/synthwave world**
  (its own `wall.css`, not the app's muted design tokens). DESIGN.md sanctions a
  single-purpose committed surface; the owner picked the neon 3D mock as the
  direction, so this divergence from the muted register is intentional.

## 7. The Phone Control screen (PWA) — the remote — **built (W3)**

The **mobile-first Control screen** (`frontend/src/screens/ControlScreen.tsx`, a
`Launcher` tile), the "remote" the kids hold. Interactive mock:
`../mocks/jpet/07-phone-control.html`. It subscribes to `api.petStream` (SSE) for
live status and sends `POST /api/pet/command`. Shipped in W3: live status +
care buttons + the send-it-somewhere room map. (The **talk bar + voice** ride in
with the `pet.turn` brain in W4 — the `say` command doesn't exist until then.)

- **Live status**: the pet's name, mood face, and Food/Energy/Fun/Love bars,
  updating in real time as the Wall (or the tick) changes them.
- **Care buttons**: Feed / Play / Pet / Sleep — same commands the Wall exposes.
- **Send it places**: a small top-down map of the room; tap a spot → `move`
  command → the Wall's robot walks there. ("Come here", "go to your bed".)
- **Talk / tell it to do things**: a text box (and Web Speech mic on W6) → a `say`
  command → `pet.turn` → the pet answers in character on the Wall and on the phone.
- Built mock-first (`dev:mock`) against fixtures, then wired to `api/client.ts`.
- Fits the PWA's existing mobile shell, offline outbox, and device-session auth
  (the phone is a kid/family device session, §3).

## 8. LLM configurability — the JPet routing rows (built, W4)

The pet's brain is a **first-class, owner-routable LLM option**, not a buried
constant:

- `pet.turn` and `pet.thought` are registered in `TASK_DEFAULTS` /
  `TASK_REASONING_BUCKET` (low reasoning — pet chat should be snappy) with
  `TASK_LABELS` entries ("JPet — reply" / "JPet — idle thought"). The
  `LLMSettingsScreen` **lists every `TASK_DEFAULTS` key**, so they surface
  automatically with the same per-task provider:model picker — no bespoke card UI.
- Default route is `xai:grok-4.3` (like every task, so it works out of the box);
  the owner points either at the **on-box local model** from that row (or via
  `JBRAIN_LLM_TASKS`) to make the pet free/private and keep it in second seat.

## 9. Waves

Each wave is independently mergeable; tests land with code (80% backend / 100%
security), CI green before merge (non-negotiables #5–#6). Frontend waves carry
Vitest coverage and are built mock-first.

- **W0 — Backend safety spine.** ✅ **Landed** (migration 0123). `app.pet_state`
  table + owner-only, domain-firewalled RLS + isolation test (a health-narrowed
  session sees only its pet; a non-owner kid/device principal sees none; a
  cross-domain insert is rejected); the pure drive math (`jpet/service.py`, unit-
  tested); the drives tick (`jpet/scheduler.py`, arithmetic-only asyncio loop wired
  into the app lifespan, off the job queue) with a real-Postgres tick test. *Exit
  met: drives advance on a clock; neither the pet nor a kid principal can read a
  firewalled domain.* (Dedicated kid device-session minting rides in with the
  command API in W1/W3; the firewall guarantee is already enforced + tested.)
- **W1 — Realtime backbone.** ✅ **Landed.** `GET /api/pet` + `POST /api/pet/command`
  (feed/play/pet/poke/sleep/move — pure command folding in `jpet/service.py`, applied
  by the repo) + `GET /api/pet/stream` (SSE fan-out via `PetBroadcaster`; the tick and
  every command publish, so subscribers re-render live). Owner-gated for now (the kid
  device principal joins in W3). Tests: unit command/broadcaster + real-Postgres
  command deltas + a subscriber-receives-the-command sync test + an HTTP round-trip.
  *Exit met: a command from one client updates every subscriber live — the sync
  contract both surfaces build on.*
- **W2 — The Wall (3D).** ✅ **Landed.** `WallScreen.tsx` + `petScene.ts` (the
  verified WebGL engine ported to TS — no external 3D dep), rendering authoritative
  state from `api.petStream` (SSE), client-side walk interpolation + `emotion → pose`
  + bloom; local poke/click-to-walk + care buttons emit `POST /api/pet/command`.
  Launcher tile + App route wired; `api.getPet`/`sendPetCommand`/`petStream` + mock
  fixtures added. Tested with `vi.mock`-ed scene (jsdom has no WebGL); full frontend
  suite green. *Exit met: the pet lives on the wall and obeys commands from W1.*
- **W3 — The Phone Control screen (PWA).** ✅ **Landed.** `ControlScreen.tsx` —
  mobile-first control surface: live status via `api.petStream`, care buttons, and
  the "send it here" room map (tap → `move` command). Launcher tile + App route
  wired; tested with injected deps; full frontend suite green. (Talk box moved to
  W4 with `pet.turn`.) *Exit met: a kid drives the wall pet from the phone; Wall and
  phone stay in sync off the shared stream.*
- **W4 — The brain (talk).** ✅ **Landed.** `pet.turn`/`pet.thought` registered in
  the router (`TASK_DEFAULTS`/`TASK_REASONING_BUCKET` + a `TASK_LABELS` entry, so they
  auto-surface in the LLM-settings screen — the "JPet card" is a routing row, no new
  UI). `jpet/brain.py`: a safe kids' persona + structured `{speech, emotion, action}`
  through the adapter, degrading to a friendly babble on a bad response. A `say`
  command (`/api/pet/command`) runs it, `repo.apply_reply` persists + broadcasts; a
  talk box on the Control screen and a speech bubble on the Wall. Tests: brain unit
  (faked LLM) + `apply_reply` PG + the talk-box screen test. *Exit met: a child tells
  it something and it answers in character on both surfaces.* (Default route is
  `xai:grok-4.3` like every task — the owner points it at the local model via the
  JPet routing row; `JBRAIN_LLM_TASKS` also works.)
- **W5 — It's alive.** Idle action-selection + occasional `pet.thought` (skippable
  under load); `app.pet_memory` (RLS + isolation test) fed back into prompts (the
  Generative-Agents loop); the curated firewalled environment feed; autonomous
  behaviours (wander, seek food when hungry, sleep at night). *Exit: the pet acts
  on its own, remembers recent interactions, and reacts to a safe digest of the
  house.*
- **W6 — Voice + polish.** Web Speech STT on the phone (talk to it out loud) + a
  TTS/babble voice on the Wall; day/night lighting; care-loop stakes (neglect →
  visibly sad); Wall kiosk mode + phone↔wall pairing. *Exit: the kids talk to it
  by voice and it feels alive and cared-for.*

**Ordering:** W0 → W1 are the blocking spine (safety + sync). W2 (Wall) and W3
(Phone) both build on W1 and can proceed in parallel; W4 needs W1 + one surface.
W5/W6 layer on top.

## 10. Open decisions (settle before promotion)

- **One pet or per-child pets?** Schema anchors on `subject_id`; single pet is the
  v1 default. Per-child is more rows, no new machinery.
- **Command authority granularity.** Is every family device allowed every command,
  or do some (e.g. rename the pet) stay owner-only? Default: kids get care + talk +
  move; owner gets config.
- **Tick cadence vs. movement smoothness.** Movement wants a faster publish cadence
  than pure drives (~5 s vs ~30 s); confirm the tick/stream rate that feels alive
  without being chatty on the wire.
- **Idle-thought budget.** How often the pet spends a `pet.thought` call; a
  conservative default (every few minutes, skipped under load) keeps it free.
- **Environment feed contents.** Which safe signals the digest carries
  (time-of-day, note-activity volume, weather) — must stay inside the pet
  principal's scope by construction.
- **Voice on by default?** Web Speech TTS is free but can be startling; likely
  default off, opt-in per wall.
