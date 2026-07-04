# JBrain2 — JPet: the wall pet (a robot avatar for the box)

> **Status:** Proposed · **Last verified:** 2026-07-04 · **Waves:** W0◻️ W1◻️ W2◻️ W3◻️ W4◻️

A **local wall option**: a webpage that shows a window into a room, and inside
the room a robot — a synthwave/Tron wireframe avatar wired to an LLM. Kids can
poke it with the mouse, tell it to do things, and talk to it. It gets *hungry*,
*sleepy*, *bored*, and *moody* like a virtual pet, and it is fed a curated,
firewalled trickle of what the house is doing. The pet is a **companion surface
on top of the existing box**, not a new brain: it runs beside JBrain and always
**takes second seat to the app's real processing**.

This doc is `Proposed` (icebox). Nothing is built. Promoting it means giving it a
roadmap slot and reconciling every wave against the `CLAUDE.md` non-negotiables —
which this plan is already written to satisfy.

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
   `{ speech, emotion, action }`.

The one historically-famous pet that *did* ship a real neural net — *Creatures*
(1996) — is a footnote; nobody builds it that way now because an LLM gives richer
behaviour with zero training data. So the whole feature is a **frontend render +
a drives table + a thin LLM route**, all on substrate we already have.

### Prior work we lean on (patterns, not dependencies)

- **"Generative Agents: Interactive Simulacra of Human Behavior"** (Park et al.,
  2023) — the canonical LLM-agent-with-memory loop (*memory stream → reflection →
  planning*). JBrain already has RAG + a memory system, so the pet's "it
  remembers you fed it yesterday" comes almost free (W3).
- **The Sims motive engine** — the drive-decay math and idle action-selection
  (when bored, pick the action that most relieves the worst need).
- **Tamagotchi** — the care-loop stakes (neglect → sad/sick) that make kids
  attach.
- **Live2D / VTuber avatars** — an LLM face that talks with personality + TTS.
- **Three.js + `UnrealBloomPass`** — the render path for the Tron look (W1).

## 1. What already exists (reuse, don't rebuild) — cited

- **LLM adapter + task-profile router** (`llm/router.py`): every call runs under a
  named task in `TASK_DEFAULTS` (task → `provider:model`), individually routable
  via `JBRAIN_LLM_TASKS`, with a per-task reasoning bucket
  (`TASK_REASONING_BUCKET`). The pet adds **`pet.turn`** (interaction) and
  **`pet.thought`** (idle daydream) tasks — never a provider SDK (non-negotiable
  #1). Structured output via `json_schema` gives the `{speech, emotion, action}`
  shape with no parsing.
- **On-box local model** (`llm/local_gateway.py`, `llama_swap_config.py`,
  `residency.py`): the pet routes to the local gateway by default so it never
  spends API budget and never contends with the paid path — this is the mechanism
  that makes "second seat" true by construction (§4).
- **Scheduler tick loops** (`tasks/scheduler.py`, `TICK_INTERVAL_SECONDS`): the
  lightweight asyncio driver pattern the drives loop copies — no new execution
  machinery, and deliberately **off the single-threaded job queue** (§4).
- **SSE streaming** (`api/agent.py`): the existing `text/event-stream` transport
  (`text_delta`/`done`) that the pet chat reuses — no WebSocket needed (the pet
  is not the jcode terminal).
- **RLS + isolation-test pattern** (`db/session.py`, `test_lists_rls.py`): every
  new pet table ships `ENABLE/FORCE RLS` + an isolation test (non-negotiable #3).
  This is also the kids-safety firewall (§3).
- **Storage abstraction** (`FsBlobStore`): any pet asset (a captured "photo" the
  pet takes, a drawn doodle) is content-addressed by sha256 — never raw paths
  (non-negotiable #2).
- **Frontend** (React 18 + Vite + TS PWA, `frontend/src/screens/`, `Launcher`):
  the pet is a new full-screen **Wall** screen + a launcher tile. LLM routing is
  surfaced as a **new "JPet" card in `LLMSettingsScreen`** (§5), grouping the
  `pet.*` tasks alongside the existing task cards.

## 2. The drives model (new table, pure arithmetic)

**`app.pet_state`** — one row per pet (start with one pet; `subject_id` anchors
it). Columns: `id`, `subject_id`, `name`, `hunger` / `energy` / `boredom` /
`social` (0–100 floats), `mood` (derived enum, materialized for cheap reads),
`asleep` (bool), `last_tick_at`, `created_at`, `updated_at`. RLS-scoped +
isolation-tested.

The **tick** (owner-configurable cadence, ~30–60 s) advances each drive by
`elapsed × rate`, clamps to 0–100, recomputes `mood`, and flips `asleep` on the
day/night schedule or when `energy` bottoms out. **No LLM in the tick** — it is a
few multiplies and one UPDATE, so it is effectively free and never queued.
Interactions (feed, play, pet) are small deltas applied on the same row.

Idle **action-selection** (W3) is a tiny rule, not a model: when no child is
interacting, pick the action that most relieves the worst drive (wander to "eat"
when hungry, dim the lights and sleep at night), and only *occasionally* spend a
`pet.thought` LLM call to narrate it.

## 3. Kids-safety is a firewall problem (and we already have the firewall)

The pet is fed "information from the environment," and the audience is children —
so the hard rule is: **the pet must never see the health, finance, or location
domains.** This is not new machinery; it is exactly what Postgres RLS + the domain
firewall exist for (non-negotiable #3).

- The pet runs under a **dedicated low-privilege principal** scoped to a
  deliberately narrow, safe set of domains (e.g. a general/family domain only).
- Its environment feed is a **curated digest** ("it's evening; the house has been
  quiet") assembled *inside* that scoped session, so out-of-scope facts are
  invisible at the query layer, not filtered in app code.
- **`pet.turn` / `pet.thought` prompts are built from that scoped session only.**
  An isolation test asserts the pet principal cannot read a firewalled domain —
  security path, 100% coverage (non-negotiable #5).

## 4. "Second seat to the app's real processing" — the honest design

The job queue (`queue.py` / `worker.py`) is **single-threaded, FIFO by
`run_after`, with no priority column** — multi-worker/priority is explicitly
deferred (`archive/WORKFLOW_ENGINE_PLAN.md` §7). So a "low-priority background
lane" is not expressible on the queue today. **The pet sidesteps the whole
problem instead of adding a scheduler tier:**

1. **Drives tick = arithmetic in the web process** → never enqueued, ~free.
2. **Pet LLM calls default to the on-box local model** → they never touch the
   paid API and never compete with real JBrain jobs on the shared worker.
3. **The idle `pet.thought` call is skippable** → gated on local-model
   availability; if the box is busy, drop the daydream (the local gateway's
   residency check / a `defer()`-style yield). Kids never notice a missed
   thought; real work is never delayed.

If we ever want pet work *on* the queue with true deprioritization, that is
net-new (a priority term in `claim()`'s `ORDER BY`, or a separate low-priority
worker/lane) — noted, but **out of scope for JPet**. The design above gives
"second seat" without it.

## 5. LLM configurability — a "JPet" card in settings

Per the owner decision, the pet's brain is a **first-class LLM option under a new
JPet card** in `LLMSettingsScreen`, not a buried constant:

- `pet.turn` and `pet.thought` land in `TASK_DEFAULTS` / `TASK_REASONING_BUCKET`,
  **defaulting to the local model** at a low reasoning bucket (cheap, fast,
  private).
- The JPet settings card lists those tasks with the same per-task
  provider:model picker the other task cards use (backed by `JBRAIN_LLM_TASKS`),
  so the owner can promote the pet to a cheap cloud model per-environment without
  code changes.
- Reasoning stays low by default — pet chat should be snappy, not deliberative.

## 6. The render — Tron/synthwave wireframe (frontend only)

A new full-screen **Wall** screen under `frontend/src/screens/`, launched from a
`Launcher` tile. Self-contained WebGL; it does not hit the Python backend to
render.

- **Three.js** (new frontend dep — added to `package.json` **and**
  `scripts/dev-setup.sh` in the same wave, non-negotiable #8).
- Wireframe materials (`wireframe: true`) on a low-poly robot rig + room box;
  `UnrealBloomPass` for the neon glow; a scrolling grid floor; magenta/cyan point
  lights — the synthwave palette from `reference/DESIGN.md`'s dark-first tokens.
- Simple rig animation: idle bob, blink, head-turn-to-cursor, and an
  `emotion → pose/face` map driven by the LLM's structured `emotion` field.
- Honors the DESIGN.md system (dual theme, tokens); a binding mock lands under
  `docs/mocks/` before the screen is built (DESIGN.md gate).

## 7. Talking to it — reuse SSE + the browser

- **Text chat** streams over the existing `api/agent.py` SSE transport
  (`text_delta`/`done`); the pet endpoint is a thin sibling that runs `pet.turn`
  with the pet's state + scoped feed in the prompt.
- **Voice** uses the **browser Web Speech API** (STT + TTS) — zero backend work,
  ideal for a wall display. A nicer local-gateway voice is a later upgrade, not a
  W-gate.

## 8. Waves

Each wave is independently mergeable, tests land with code (80% backend / 100%
security), CI green before merge (non-negotiables #5–#6). Frontend-only waves
carry Vitest coverage.

- **W0 — Drives spine + safety (backend).** `pet_state` table + migration + RLS
  isolation test; the tick loop (arithmetic only); the scoped **pet principal**
  and its firewall isolation test (security path). No LLM, no render yet. *Exit:
  drives advance on a clock; the pet principal provably cannot read a firewalled
  domain.*
- **W1 — The wall (frontend spike).** Three.js wireframe robot on a bloom-lit
  grid, mouse-look, click-to-poke reactions wired to W0's drives; mood face from
  the materialized `mood`. `package.json` + `dev-setup.sh` updated together; mock
  filed under `docs/mocks/`. *Exit: the kids can see it and poke it; the
  aesthetic is signed off.*
- **W2 — It talks.** `pet.turn` task + the JPet settings card; a thin SSE pet
  endpoint; text chat box; structured `{speech, emotion}` drives the face.
  Defaults to the local model. *Exit: a child types, the pet answers in character
  and its face changes.*
- **W3 — It's alive.** Idle action-selection + occasional `pet.thought`
  (skippable under load); `pet_memory` table (RLS + isolation test) fed back into
  prompts (the Generative-Agents loop); the curated firewalled environment feed.
  *Exit: the pet acts on its own, remembers recent interactions, and reacts to a
  safe digest of the house.*
- **W4 — Voice + polish.** Web Speech STT/TTS; day/night lighting; care-loop
  stakes (neglect → visibly sad). *Exit: the kids talk to it out loud at the
  wall.*

W0 is the blocking wave — it establishes the safety firewall the rest depends on.
W1 can proceed in parallel with W0 (frontend-only), joining at W2.

## 9. Open decisions (settle before promotion)

- **One pet or per-child pets?** The schema anchors on `subject_id`; single pet is
  the v1 default. Per-child adds rows, no new machinery.
- **Idle-thought budget.** How often does the pet spend a `pet.thought` call? A
  conservative default (e.g. once every few minutes, skipped under load) keeps it
  free; owner-tunable via the JPet card's reasoning/cadence.
- **Environment feed contents.** Exactly which safe signals the digest carries
  (time-of-day, note-activity volume, weather) — must stay inside the pet
  principal's scope by construction.
- **Voice on by default?** Web Speech TTS is free but can be startling; likely
  default off, opt-in per wall.
