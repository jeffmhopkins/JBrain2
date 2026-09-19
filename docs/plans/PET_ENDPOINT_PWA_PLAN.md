# Pet endpoint — the PWA build, ahead of the hardware

> **Status:** In progress · **Last verified:** 2026-09-13 · **Waves:** P1✅ P1a✅ P1b✅ P4✅ P2🟡 P0◻️ P3◻️ P5◻️

Build the **room endpoint as a PWA surface first** — a full-screen pet the child talks to and
touches, running on any phone or spare tablet — so the whole product exists and is being used
before the two Waveshare ESP32-S3 panels arrive. Splits out of
`ROOM_ENDPOINT_PLAN.md`, which keeps only the firmware, audio and OTA waves.

Binding mock (chosen, `../reference/DESIGN.md`): `../mocks/room-endpoint/pet-face.html`.

## 1. Why this order is right, and not just impatience

1. **Everything above the transport is device-independent.** The face, the six emotions, the limb
   rig, the touch model, the anti-boredom engine and the voice round-trip are not ESP32 concerns.
   Build them here and the firmware wave shrinks to *port a renderer and wire I2S* — which is the
   only part where the hardware gets a vote.
2. **It closes the biggest open risk now.** The largest unknown in the whole line of work is
   whether a wake word and Whisper actually hear a four-year-old (no false-reject rate has ever
   been published for ages 3-5). **A phone has a better microphone than the panel will.** Put it
   in front of him this week and the answer arrives before any firmware is written.
3. **It is a product, not scaffolding.** A spare tablet in his room running the pet is worth
   having even if the ESP32 boards never work out. Nothing built here gets thrown away.
4. **It separates the design from the panel.** 29 × 35 mm is tiny. Running the *same* design at
   tablet size tells us whether a weakness is the design or the millimetres — which we cannot
   learn from the hardware alone.

## 2. How much of this already exists

More than it looks. This is mostly assembly.

| Piece | Status |
|---|---|
| Server-authoritative pet, 19 actions, 12 colours, 7 forms, LLM-free intent router | **shipped** (`jpet/`) |
| Live fan-out to any number of surfaces | **shipped** (`jpet/broadcast.py`) |
| `getPet` / `sendPetCommand` / `petStream` (SSE) typed client | **shipped** (`frontend/src/api/client.ts`) |
| A child-facing play surface, big touch-DOWN buttons, push-to-talk | **shipped** — `ControlScreen.tsx` is already "the mobile remote the kids hold" |
| Kokoro TTS + whisper.cpp STT | **shipped** (`tts-stt`) |
| The face renderer itself | **drawn** — the binding mock is ~600 lines of canvas that ports almost directly |
| Non-owner scoped-principal auth | **shipped pattern** — `intake_link` (`api/intake.py`): the link secret *is* the credential, binding a non-owner principal scoped to one thing |

Net-new: one screen, one scoped principal, a server-side STT path, and the endpoint shell.

## 3. The decision this plan must get right: what the tablet is allowed to be

**A tablet in a four-year-old's room must never hold an owner session.** An owner session reaches
the whole knowledge base; this device needs four verbs and nothing else.

So the endpoint authenticates as a **non-owner `pet_endpoint` principal**, minted as a
revocable link from Settings exactly like `intake_link` — the secret is the credential, shown
once, revocable, one per device. Its entire scope:

- read pet state (subscribe + poll),
- send a pet command,
- post a short audio clip for transcription,
- fetch TTS audio.

Nothing else. Per non-negotiable #3 the new scope gets an **RLS isolation test** proving a
`pet_endpoint` principal cannot read notes, wiki, health, finance or location rows.

## 3a. What shipped first, and the scope change that made it smaller

The owner's instruction was narrower than §3 assumed: **an owner-only surface in their own PWA,
for troubleshooting interaction and visuals**. That removes the plan's hardest wave from the
critical path — a screen the owner reaches behind their own session needs **no `pet_endpoint`
principal at all**. P0's auth work stays in the plan because the moment this runs on a tablet in
the child's room it is required again; it is descoped from *this* surface, not cancelled.

Landed (`frontend/src/pet/`, `frontend/src/screens/PetFaceScreen.tsx`):

- **P1 ✅ the face.** `pet/face.ts` (the ~17-float procedural face, six emotions as lid geometry,
  asymmetry for curious and silly), `pet/rig.ts` (the limb rig and figure transform),
  `pet/draw.ts` (the canvas renderer at the panel's native 368×448). Framework-free on purpose:
  these are the reference implementation the ESP32 port transcribes.
- **P4 ✅ the anti-boredom engine.** `pet/variants.ts` — weighted pools, per-variant cooldowns,
  repetition penalty, with the boundary enforced: the *action* is the server's, only the
  *variant* is local.
- **P1a ✅ the script player**, added after the first deploy: commands reached the box and the
  box answered, but the panel played none of it — it read only `script[0].emotion` for the face,
  so typing "dance" changed nothing visible. The box's script is now performed step by step,
  honouring each step's `duration_ms` and wearing the emotion of the step ACTUALLY playing. A
  script arriving on the *stream* plays too, so a command from the phone Control screen or an
  automation is visible here. `jpet/` speaks a room's vocabulary and this endpoint is a face with
  no room, so `rig.ts:actionForServerStep` maps it deliberately and falls back to a visible
  acknowledgement — never to silence, because on a panel a child is holding, "nothing happened"
  is indistinguishable from "it's broken".
- **P1b ✅ the pet's voice**, found the same way: "tell me a joke" is not a keyword, so the box
  takes the LLM path and answers with **speech** plus a small emote. The screen rendered neither.
  The reply is now **spoken** (`screens/speech.ts:speak`, the same pet-ish voice the phone
  Control screen uses) — because the audience cannot read, so a joke shown as a caption has not
  been told — once per new line, and never for the snapshot's stale line on open. A `say` in
  flight now wears a thinking face, since the LLM path takes seconds and a pet that sits idle
  through it reads as not having heard.
- **P2 🟡 the touch model.** One whole-screen target, one gesture, no thresholds, with the
  immediate flinch on contact. Still missing for a real endpoint: full-screen kiosk, wake lock,
  and no way out of the screen.
- **44 unit tests**, the first of which asserts the thing this whole surface exists to fix —
  that no two emotions resolve to the same parameters.

It speaks only the APIs the hardware will speak: `GET /api/pet`, `GET /api/pet/stream`,
`POST /api/pet/command`. Two findings from wiring it for real:

1. **`PetState` in the typed client was missing every ephemeral effect** (`pet_form`,
   `pet_scale`, `pet_scene`, `object_colors`, `object_scales`). They are on the wire for
   `GET /api/pet`; the interface simply never declared them, so no PWA surface could see the
   creature form. Now declared, with the SSE gap documented on the type itself.
2. **The stream/poll split is a workaround, not a design.** Because `/api/pet/stream` drops
   those effects, the screen polls `GET /api/pet` at 1 Hz purely to notice a form change —
   exactly the workaround the wall carries. P0 should delete both.

## 4. Waves

| Wave | What | Done when |
|---|---|---|
| **P0** | **The seam.** `pet_endpoint` principal + scoped link mint/revoke in Settings, the four-verb scope, RLS isolation test. **Also fix `GET /pet/stream` to carry ephemeral effects** (form/colour/scale/scene are dropped today, which is why the Wall polls at 1 Hz) so the endpoint can subscribe instead of poll. | A minted link renders the pet and can reach nothing else; the Wall can drop its poll. |
| **P1 ✅** | **The face.** Port the mock into a React component: the ~17-float procedural face, six emotions as lid geometry, the limb rig, tween-by-halving, blink/saccade/breathing, the gag structure with the bewildered hold. The pure parts — emotion resolution, rig poses, variant selection — are plain functions and get unit tests with no DOM. | The six emotions are distinguishable in a test that asserts on resolved parameters, not pixels. |
| **P2 🟡** | **Endpoint mode.** A full-screen route with no app chrome, wake lock, no navigation away, and the settled touch model: **one whole-screen target, one gesture, no thresholds** — contact opens the mic and flinches toward the finger inside 100 ms; release acts on speech, or pokes. | A child can use it without leaving it, and cannot reach the rest of the app. |
| **P3** | **Voice.** Mic → the box's own whisper, replacing the browser Web Speech path (`screens/speech.ts` ships his voice to Google in Chrome). Kokoro out. Repetition-aware fallback, **partial understanding** ("Turn what colour?") rather than "sorry, I didn't get that", and **never end on silence**. | He can say "turn red" and "be a dragon" and it works, or fails in-character. |
| **P4 ✅** | **The anti-boredom engine.** Weighted-random variant pools, per-variant cooldowns, repetition penalty. **Boundary:** the *action* stays server-authoritative; only the *variant* is chosen client-side, because it is presentation. Recency is suppressed **within** a gag, never the gag itself — preschoolers love repetition. | The tenth rapid poke differs from the first, and the log shows why. |
| **P5** | **Live trial + safety.** Put it in front of the child. Count wake-word hits and misses, ASR accuracy, and what he actually does for a week. Ship the safety controls with it: volume cap, quiet hours cutting **luminance** not just volume, and an unmistakable recording indicator. | We have a number for the thing nobody has published, and a parent-facing control that enforces it. |

P5 is a **gate on the hardware plan**, not a postscript: if the wake word cannot hear him on a
phone's microphone, it will not hear him on the panel, and `ROOM_ENDPOINT_PLAN` W6 needs to be
press-to-talk-first before a line of firmware is written.

## 5. Carried constraints (from `../reference/DESIGN.md`, settled)

- **Never a long-press on a preschool surface** — 4-5 year olds tap for up to 4.2 s.
- **Text is a debug channel.** Every state must be expressible in animation, non-speech audio or
  speech. He cannot read.
- **Emotion is lid geometry, motion and timing — never colour.** Colour is his to choose.
- **Nothing firewalled renders here**, and on a shared-room surface that includes notification
  bodies.
- **A recording indicator is mandatory** (ICO Children's Code) and must be a whole-panel state,
  not a caption.

## 6. Safety numbers to enforce, not restate

- **65 dB(A)** close-to-ear cap (ASTM F963 / EN 71-1); parent-lockable maximum (WHO-ITU H.870).
- **Bright light in the hour before bed suppressed melatonin ~88% in 4-year-olds, persisting 50
  minutes.** Quiet hours must cut luminance and end well before bedtime.
- **COPPA:** transcribing a command and discarding the audio is the narrow carve-out. Retaining
  his voice to fine-tune — which roughly halves child ASR error — needs verifiable parental
  consent. Default to discard; make retention an explicit, separate decision.

## 7. Three shipped defects this work should fix on the way

1. `fire`, `lay`, `cleanup`, `cleanup_bed` are in `CANNED_SCRIPTS` but absent from
   `CommandAction`, so `POST /pet/command {"action":"fire"}` is a 422 — reachable only via `say`.
2. `GET /pet/stream` drops every ephemeral effect (P0 above).
3. `hide` renders identically to `sit` — it walks to a corner and squats with nothing occluding
   it. The mock shows what it should be: **hands over the eyes.** Peekaboo is squarely on-target
   for this age, and it is currently a dud.

## 8. Out of scope

Firmware, I2S audio, OTA, the panel's physical enclosure — all stay in
`ROOM_ENDPOINT_PLAN.md`. No new pet *behaviour*: this plan renders and routes what
`jpet/` already owns, and never invents pet state.
