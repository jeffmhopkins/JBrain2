# Room endpoints — the box's face and ears on a small AMOLED satellite

> **Status:** Scheduled · **Last verified:** 2026-09-19 · **Waves:** W1◻ W2◻ W3◻ W4◻ W5◻ W6◻ W7◻

**The hardware arrived 2026-09-18** and the owner confirmed the two constraints that decide
the whole delivery path: the panels sit on **the same LAN as the box**, and the box's own USB
port is available **for the first flash only** — every update after that must arrive over
Wi-Fi. So this promoted out of `proposed/`: W1 is designed (§10) rather than sketched, and the
plan is against a device on the bench rather than a listing. Supersedes `../archive/DITOO_PLAN.md`,
which chased the same goal through Bluetooth and paid for it; **the Ditoo itself is out of the
picture** — cancelled, not retained as a speaker — and that doc is archived for its findings,
not its design.

> **Split, 2026-09-13.** The device-independent half — the face, the touch model, the voice
> round-trip, the anti-boredom engine — moved to `PET_ENDPOINT_PWA_PLAN.md` and is being
> built as a **PWA surface first**, so the product is in use before the panels arrive. What stays
> here is firmware, I2S audio, OTA and the enclosure. That plan's P5 live trial **gates W6**: if a
> wake word cannot hear a four-year-old on a phone's microphone, it will not hear him on this panel.

## 1. The hardware, as ordered

**2 × Waveshare ESP32-S3-Touch-AMOLED-1.8**

| | |
|---|---|
| Display | 1.8″ **AMOLED**, **368×448**, 16.7 M colours, SH8601 driver, FT3168 capacitive touch |
| SoC | ESP32-S3, Xtensa LX7 dual-core @ 240 MHz, 512 KB SRAM |
| Memory | **8 MB PSRAM**, **16 MB flash** |
| Radio | 2.4 GHz Wi-Fi (b/g/n) + BLE 5 |
| Audio | onboard codec, **microphone + speaker** |
| Also | 6-axis IMU, RTC, PMU, TF slot, optional 3.7 V battery (~1 h screen-on, ~6 h low-power) |
| Docs | Waveshare wiki + `waveshareteam/ESP32-S3-Touch-AMOLED-1.8` sample code |

Three properties of this specific board carry the design:

1. **AMOLED, not LCD.** True blacks mean pixel art on a black field reads as *emitting* pixels
   rather than as a photograph of a pixel display. This is the closest a screen gets to the
   LED-matrix look that started this whole thread, and it was a lucky pick.
2. **368 ÷ 16 = 23 exactly.** A 16×16 canvas renders at 23 px per cell with **no scaling and
   no fractional pixels**, filling 368×368 and leaving a **368×80** strip beneath for a
   caption, clock or status line. (32×32 → 11 px cells centred with 8 px margins; 64×64 →
   5 px cells.) Pixel art was the requirement; the grid maths is exact.
3. **8 MB PSRAM.** Framebuffers, audio buffers and an on-device wake-word model all fit
   without the contortions a 4 MB, no-PSRAM board would have forced.

## 2. Why this deletes most of the Ditoo plan

The entire §4 of `../archive/DITOO_PLAN.md` — the BT-radio presence gate, the network-namespace
constraint, a containerised BlueZ, host module loads, a D-Bus pairing UI — existed because
the *box* had to speak Bluetooth. **There is no Bluetooth anywhere in this design.** The
endpoint is the endpoint; it talks Wi-Fi to the api like any other client.

Gone with it: the ~10 m range limit, the one-Bluetooth-link-at-a-time contention with the
owner's phone, the reverse-engineered protocol, and the risk that a vendor OTA breaks it.

Gained on top:

- **Full-duplex voice is actually reachable.** Espressif's **ESP-SR** runs on the S3 and
  provides on-device wake word (microWakeWord/WakeNet) *and* **acoustic echo cancellation** —
  the exact thing the Ditoo could not do for want of a DSP. Barge-in stops being a stretch
  goal. **— Wrong, corrected in §10.5-A:** ESP-SR runs on the S3, but AEC needs an echo
  reference channel and *this board has none* (ES8311, one mic, no ES7210). The wake word
  survives; barge-in probably does not.
- **Wideband audio both directions**, instead of HFP's 8/16 kHz narrowband.
- **A touchscreen**, so the endpoint is an input device too, not only a display.
- **A general screen.** It can show the pet, a caption, a chart, text — things a 16×16 LED
  matrix physically cannot.

What we give up versus the Ditoo: a 15 W speaker (this one is small — fine for speech, not
music), and the charm of a finished retro object. A case is worth planning for.

## 3. What the box already brings

Almost the whole server half exists:

- **TTS** — Kokoro in the `tts-stt` container, already serving read-aloud.
- **STT** — whisper.cpp in that same container; `jbrain/transcribe.py` is the client.
- **A pet worth showing** — `jpet/` is server-authoritative with a broadcaster
  (`jpet/broadcast.py`) that already fans state to the Wall and the phone. An endpoint is one
  more subscriber.
- **Owner notifications** — `notify/bus.py`.
- **A scheduler + action registry** — `workflow/registry.py:ACTION_SPECS`.
- **Device identity, already shipped.** `devices/repo.py`: a device is a
  `Subject(kind='device')` with a bound `Principal(kind='device_key')`, with rotation
  (`devices/service.py:rotate_device_key`) and revocation, plus a pairing flow in
  `locations/pairing.py`. **These endpoints need no new auth model** — they are devices, and
  the MQTT work already reuses the same substrate.

So the net-new surface is: **firmware**, **one protocol**, **renderers**, and an **audio
round-trip**.

## 4. The four decisions to make first

### 4.1 Transport: WebSocket to the api, or MQTT via the shipped broker

Both are already in the building. MQTT (`mqtt` compose profile, Mosquitto + go-auth
delegating every connect/publish/subscribe to the api's `/internal/mqtt-*` endpoints, already
keyed on `device_key`) is the better fit for **state fan-out** — one topic per endpoint, the
broker handles reconnects, and a second unit costs one more topic. A **WebSocket** to the api
is the better fit for **audio streaming**, which is a continuous bidirectional byte stream
that MQTT models badly.

Recommendation: **both, split by traffic type.** MQTT for display frames, pet state and
notifications; one WebSocket per endpoint for the voice turn. Decide in W1 — retrofitting the
audio path is the expensive mistake here.

### 4.2 Two units means multi-endpoint from day one

They ordered two, and that is a gift: **design the protocol for N endpoints now** rather than
retrofitting device identity later. Every message is addressed to a device id; every renderer
takes "which screen"; the Settings UI lists endpoints. Cheap now, invasive later.

The second unit also has an operational job — see §4.4.

### 4.3 Firmware toolchain

Waveshare ships ESP-IDF sample code and a wiki for this exact board, which is the safe path
(ESP-IDF + LVGL for the UI, ESP-SR for wake word + AEC). **ESPHome** is the tempting
alternative — far less code — but two things must be checked before betting on it: whether
its display stack supports the **SH8601** AMOLED driver, and that its `voice_assistant`
component speaks to Home Assistant rather than to us, so we would be implementing that
protocol server-side. Verify in W1; do not assume.

### 4.4 Rule 10 says OTA is a W1 feature, not a later one

The owner runs this box remotely with **no terminal** (`CLAUDE.md` #10). If firmware updates
need a USB cable, every change is a physical trip and the whole thing rots. So **OTA from the
PWA is part of the first wave**, not a nice-to-have — and the box already has the shape for
it (Ops → Update).

This is where the second unit earns its keep: keep one as the **bench unit** that can be
bricked and re-flashed over USB, and one as the deployed unit that only ever takes OTAs that
worked on the bench. Never OTA both at once.

## 5. The safety surface is bigger than the Ditoo's

A screen in a room is a **disclosure** surface; a screen *with a live microphone* is also a
**capture** surface, and this box has domain firewalls (`CLAUDE.md` #3) precisely because
some of its content must not leak sideways. Non-negotiables:

- **Nothing behind a domain firewall renders on an endpoint by default.** Health, finance and
  location content must be opt-in per device, never the default. A content-free "3 things
  need you" is safe; the body is not.
- **The microphone needs a hard, visible mute** and an unambiguous listening indicator. The
  Wall's pet is used by a child (`deploy/wall/pet.html`); an always-on mic in a family space
  is a decision to take deliberately, in the open, not by default.
- **Endpoints are devices, not owners.** They authenticate with `device_key`, they get their
  own RLS scope, and a compromised endpoint must not be able to read the knowledge base.
  Every new table gets an isolation test (#3).
- **Wake word stays on-device.** ESP-SR means no audio leaves the endpoint until the wake
  word fires — which is both a privacy property and a bandwidth one. Do not fall back to the
  browser's Web Speech API path the Wall currently uses; that ships audio to Google
  (`frontend/src/screens/speech.ts`), and fixing it is arguably a prerequisite, not a
  follow-up.

## 6. Wave sketch

| Wave | What | Notes |
|---|---|---|
| **W1** | **Bench bring-up + decisions.** Flash Waveshare's sample, confirm display/mic/speaker, settle §4.1 transport, §4.3 toolchain, and get **OTA** working. | **Designed in §10** — the hardware arrived and the owner set the constraints. |
| **W2** | **Protocol + device identity.** N-endpoint addressing on the shipped `device_key` model, `endpoint_url`/broker config defaulting to empty so the feature is simply absent when unset (the `sdr_url` pattern), Settings → Endpoints. | No new auth model. |
| **W3** | **Display path.** Frame/scene protocol, renderers, notification cards. **GUI round 1 is drawn**: `../mocks/room-endpoint/device.html` — four shapes on one state model, awaiting the owner's pick. | See §8: the mock measured the panel and moved the goalposts. |
| **W4** | **JPet on the endpoint.** One more `PetBroadcaster` subscriber + a sprite renderer. | The payoff. |
| **W5** | **Voice out.** Kokoro → endpoint over the audio channel, with a `speak` action. | |
| **W6** | **Voice in.** ESP-SR wake word on-device → stream to whisper → the agent loop. Half-duplex first; enable AEC barge-in once the loop is honest. | |
| **W7** | **Workflow action + owner-scope agent tool**, behind the §5 content fence. | |

Out of scope for now: the camera-less board's IMU and TF slot, battery operation (mains-power
it; 1 h screen-on is not a product), and music playback.

## 7. Risks

- **Firmware is a new maintenance surface** for a project that has none today. OTA (§4.4) and
  the bench unit are the mitigations; neither is optional.
- **ESP-SR's AEC quality on this specific board is unproven by us** — one mic, a nearby
  speaker, a small enclosure. If barge-in is bad, half-duplex still works and is honest.
- **Wi-Fi audio latency** determines whether the thing feels alive or laggy. Measure in W1.
- **1.8″ is a desk-distance display**, not a read-across-the-room one. Placement matters, and
  it argues for the caption strip carrying few, large glyphs.
- **A no-name listing risk that did not materialise:** this is Waveshare, with a wiki,
  schematics and sample code. Keep it that way — do not substitute an unbranded clone for
  unit three.

## 8. What the mock round changed (2026-09-13)

`../mocks/room-endpoint/device.html` simulates the ordered panel at its real geometry, and the
first thing it produced was a correction to this plan:

> **1.8″ at 368×448 = 29.0 × 35.3 mm, 322 ppi** — a smartwatch panel, about a postage stamp.

Three things written above are wrong in light of it, and are superseded by this section:

1. **The "368×80 caption strip" is 6.3 mm tall.** It holds one short line at a legible size. §1
   called it a place for "a caption, clock or status line"; it is not a status bar and cannot
   become one.
2. **"The box's face and ears in a room" overstates the display.** At 322 ppi the panel is
   unreadable beyond arm's length. The *ears* are a room device; the *face* is a desk object.
   That may mean the two units want different shapes, or that the display's job is presence
   and state rather than information.
3. **Type has a floor.** 12 px is 0.95 mm. Nothing below ~28 px (2.2 mm); anything that matters
   wants 40 px+. One thing at a time, large.

The round offers four shapes — **A Matrix** (a 16×16 LED panel), **B Porthole** (a full-res
creature that tracks your finger), **C Face** (one enormous state-driven thing), **D Room** (a
2D echo of the Wall's room, where tapping a prop issues the shipped command). They differ in
what the panel is *for* and what touch *means*; each states its own cost. See
`../mocks/room-endpoint/README.md` for the open questions the owner is being asked to settle.

W3 is blocked on that pick, and W4 (JPet) inherits it — the shape decides whether the pet is
the host, a guest, or a resident.

## 9. Round 2 — the pet is for a four-year-old (2026-09-13)

The owner narrowed the brief: no needs or hunger system, just a robot pet that has clear
emotions, changes colour and animal form on request, does funny things, and answers his
four-year-old. Three research passes (JPet contract, preschool interaction, character-expression
prior art) settled most of it. Mock: `../mocks/room-endpoint/pet-face.html`; the round's findings
and the one open question are in `../mocks/room-endpoint/README.md`.

**Nothing needs simplifying — JPet v3 already deleted the drive meters** for exactly the owner's
reason, and already ships 19 keyword-routed actions, 24 canned scripts, 12 colours plus a
rainbow, 7 creature forms, and an LLM-free intent router with canned replies and a babble
fallback. What the concept actually asks for is **one thing that does not exist**:

> **Clear emotions.** The Wall renders each of the six `EMOTIONS` as a chest-badge colour plus
> the width of one horizontal mouth bar. `curious` and `sleepy` are pixel-identical apart from
> hue; in any animal form emotion is invisible entirely; and `PetOut.emotion` is never read.

So emotion is net-new work, and this endpoint is its first surface. It is carried by lid
geometry, whole-face motion and timing — the channels that survive on a tiny panel.

### Interaction, settled

**One target, one gesture, no thresholds.** A 20 mm child touch target is 69% × 57% of this
panel, so the whole screen is the only target; and 4-5 year olds produce ordinary taps lasting up
to **4.2 seconds**, so long-press is not a gesture that exists at this age (round 1's 550 ms
mute long-press would have muted the robot on every tap). Touch means *"I'm paying attention to
you"* — a sub-100 ms flinch toward the finger, mic open while held; release acts on what he said,
or pokes him if he said nothing. **Press-to-talk is therefore free**, which matters because:

**The wake word will miss him.** Whisper is ~32% WER on child speech against ~3% for adults, and
~1% of output is hallucinated on silence and half-speech. A hallucinated command the robot *acts
on* is worse than a miss. No wake-word false-reject rate has ever been published for ages 3-5 —
**the largest measurable unknown in this plan, and cheap to close with a handful of children.**
W6 must treat "robot" as the convenience layer over the deterministic one, never the only way in.

### Waves, revised

- **W4 (JPet)** is now the emotion work, not a port: a procedural face of ~17 tweened floats, the
  six emotions as lid geometry, asymmetry for curious/silly, and the gag structure (the hold on a
  **bewildered** face is the joke — 4-5 year olds read a pratfall as funny only when the character
  looks bewildered rather than pained or smug).
- **W4b, new — the anti-boredom engine.** Weighted-random variant pools, per-variant cooldowns,
  and a repetition penalty. Loona ships 700-1000 expressions and still gets shelved; procedural
  recombination beats hand-authored volume. Suppress recency *within* a gag, never the gag itself.
- **W6 (voice in)** gains press-to-talk as the primary path, repetition-count-aware fallback
  (children repeat 79% of the time and persist through failure >75%), partial-understanding
  prompts rather than "sorry, I didn't get that", and **never ending on silence**.

### Three shipped-code defects this surfaced

1. `fire`, `lay`, `cleanup`, `cleanup_bed` are in `CANNED_SCRIPTS` but missing from
   `CommandAction`, so `POST /pet/command {"action":"fire"}` is a 422 — they are reachable only
   through `say`. A dragon-fire button cannot be built today.
2. `GET /pet/stream` frames omit every ephemeral effect (form, colour overrides, scale, scene),
   which is why the Wall polls at 1 Hz instead of subscribing. An endpoint must poll too, or the
   SSE payload needs fixing — **fix it rather than inherit the workaround**.
3. `hide` renders identically to `sit` — it walks to a corner and squats, with nothing occluding
   it. For a four-year-old playing peekaboo that is a dud, and peekaboo is squarely on-target for
   the age.

### Safety, with numbers

- **65 dB(A)** close-to-ear cap (ASTM F963 / EN 71-1); WHO-ITU H.870 wants a parent-lockable max.
- **One hour of bright light before bed suppressed melatonin ~88% in 4-year-olds, persisting 50
  minutes after lights out.** Quiet hours must cut **luminance**, not just volume, and end well
  before bedtime. An AMOLED that can go truly black should.
- **The ICO Children's Code requires a recording indicator** (its own example is a light that
  turns on when recording), so §5's "muted is a promise" is a compliance requirement.
- **COPPA:** command audio deleted promptly is a narrow carve-out. Retaining his voice to
  fine-tune — which roughly halves child ASR error, the highest-leverage fix available — needs
  verifiable parental consent. That is a fork to decide deliberately, not to drift into.

## 10. W1, settled — the bring-up path (2026-09-19)

The units are on the bench, and the owner settled the two questions W1 was really waiting on:

> **Same LAN as the box**, and **the box's own USB port is available for the first flash** —
> but only the first. Everything after that has to arrive over Wi-Fi.

That second sentence is §4.4 restated by the person who has to live with it, and it is the
constraint this section is built around. It also makes the box — not a laptop, not a browser —
the flashing seat, which turns out to be a better one: **the box can bake its own secrets in at
flash time**, which a generic web flasher cannot.

### 10.1 Generic firmware, personalised on the box

| Stage | Where | Holds |
|---|---|---|
| Build | GitHub Actions, `espressif/idf` image | `firmware.bin` — **no credentials, no token, no certificate**. Identical for both units, safe to publish as a release artifact. |
| Personalise | the box, at flash time | an **NVS blob** (`nvs_partition_gen.py`): Wi-Fi SSID + password, `https://jbrain.local`, this unit's device token, and **the box's own Caddy root certificate**. |
| Flash | the box, over USB | `esptool` writes bootloader + partition table + factory + app + NVS. |
| Update | the box, over Wi-Fi | `esp_https_ota` pulls the next `firmware.bin`, validating against the root already in NVS. OTA writes only the app slot, so the credentials survive. |

The split matters for more than tidiness. The artifact CI publishes carries nothing sensitive,
so the build can be public and cached; and the credentials never pass through a browser, a
download, or this repo. It also closes §8-era finding **C** — that reading Caddy's root out of
`proxy:/data/caddy/pki/authorities/local/root.crt` needs `docker cp`, i.e. a terminal — without
adding an operator step: **the box already has that file**, and the flash action reads it.

### 10.2 The flashing sidecar

A profile-gated sidecar beside `deploy/sdr/` — `deploy/endpoint/`, `Dockerfile.endpoint`,
profile `endpoint` — so a stock deploy never starts it, and an `endpoint_url`-shaped empty
default means the feature is simply absent when unset (the `sdr_url` pattern). It runs
`esptool`, which is pure Python over `pyserial`: **no `apt`**, which matters because the PWA
update path cannot apt (`../archive/DITOO_PLAN.md` §4.3).

`update-inner.sh` refreshes source and recreates the stack, so the compose change ships through
**Ops → Update with no host step**. One difference from the SDR precedent to verify rather than
assume: `sdr` maps `/dev/bus/usb`, which works because an RTL-SDR is a raw libusb device. The
ESP32-S3's native USB enumerates as a **kernel CDC serial port** (`/dev/ttyACM*`), which is not
under that path, and which does not exist until the board is plugged in. So this needs `/dev`
plus `device_cgroup_rules` for the tty majors instead of a static `devices:` entry — a narrower
grant than `privileged`, but a different one, and hotplug is the reason.

The operator surface is a button, per rule 10: **Ops → Endpoints → Flash**, which lists the
serial ports it can see, mints the device token, builds the NVS blob and runs the flash with
its log streamed back. Nothing about it is shell-only.

### 10.3 What makes "cable once" true rather than aspirational

An OTA that boots but cannot join Wi-Fi, or joins but cannot reach the box, strands the unit
back on the cable. Two mechanisms prevent it, and **both must be in the first flashed image** —
they cannot be added later, because the image that lacks them is precisely the one that strands
you:

1. **Rollback gated on reaching the box.** `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`, with the new
   image calling `esp_ota_mark_app_valid_cancel_rollback()` **only after** it has joined Wi-Fi
   *and* completed one round trip to the api. Anything less and the bootloader reverts to the
   previous slot on the next reset. The bar is "it works", not "it booted".
2. **A frozen factory recovery app.** 16 MB of flash affords factory + two OTA slots, and an
   invalid otadata falls back to factory. Keep that image dumb — join Wi-Fi, fetch firmware,
   flash it — and never change it.

Sketch, to be pinned in the partition CSV: `nvs` 24 K · `otadata` 8 K · `phy_init` 4 K ·
`factory` 1.5 M · `ota_0` 5 M · `ota_1` 5 M · the remainder for assets. Two full-size slots is
what buys the rollback; do not trade them away for a bigger asset partition.

### 10.4 Order of work, and what each step buys

Waveshare ships 17 numbered examples on ESP-IDF 5.5.5/6.0.2 **with prebuilt binaries and factory
recovery images in Releases**, so the first steps are flash-and-look, not write-code.

| # | Step | Proves | Source |
|---|---|---|---|
| 0 | Power both units from any charger, **before flashing anything** | the panels are alive — and preserves the ability to tell "DOA" from "my firmware is wrong" | factory image, as shipped |
| 1 | `14_lvgl_demo_v9` | SH8601 panel + FT3168 touch at 368×448 | vendor |
| 2 | `10_wifi_station` | the radio joins the house network | vendor |
| 3 | `12_i2s_codec` | ES8311 → speaker | vendor |
| 4 | **our skeleton** — partitions, rollback, NVS config, Wi-Fi, one round trip | the safety net exists before anything depends on it; **CI proves it compiles without hardware** | ours |
| 5 | the sidecar + Ops button | the first flash the owner drives themselves | ours |
| 6 | one frame of `frontend/src/pet/draw.ts` at native 368×448 | **whether the design survives 29 × 35 mm** — the question no mock can answer | ours |
| 7 | OTA, proven by shipping step 6's change to the FIELD unit over Wi-Fi | the cable is retired | ours |
| 8 | mic capture + the barge-in test | finding A below, one way or the other | ours |

Step 0 is not ceremony. After the first flash, a dead panel and a wrong firmware look identical,
and the return window is the thing being spent.

**Label the units physically, now: `BENCH` and `FIELD`.** §4.4 asks for it and step 7 is where it
pays — BENCH takes the cable and the mistakes; FIELD only ever takes an OTA that already worked
on BENCH.

### 10.5 Three findings from the board in hand

**A. There is no echo reference, so barge-in is probably not available.** The board carries an
**ES8311** codec and one microphone — no ES7210. The ES7210 on the ESP-BOX and Korvo-2 reference
boards is not there for extra microphones; it supplies the **reference channel** ESP-SR's
acoustic echo cancellation needs. Without it this is software AEC at best, and the pet likely
cannot hear its wake word while it is speaking.

This contradicts §2's "barge-in stops being a stretch goal" — that sentence assumed ESP-SR's
capability implied this board's. **It does not, and §2 is wrong on this point.** The design has
already absorbed the blow without knowing it: §9's *touch = mic open* came out of the preschool
research and makes press-to-talk the primary path with the wake word as convenience. That
decision is now load-bearing rather than merely defensible. Step 8 confirms it in two minutes —
play a TTS clip and try to interrupt it.

**B. The panel has no credential to talk to the box today.** `/pet/*` is `owner_only`, and
`/internal/pet/*` is 404'd by Caddy on every app surface, so an endpoint on the LAN cannot reach
the path the on-box wall uses. W2's device principal is therefore not deferrable past step 4's
round trip. The bench shortcut is a dedicated owner token, revoked when the spike ends — fine on
a bench, **never on the unit that goes in a child's room** (§3 of `PET_ENDPOINT_PWA_PLAN.md`).

**C. Resolved by 10.1** — the LAN certificate stops being an operator problem once the box is
the flasher.

### 10.6 Still open after this section

- **Does `/dev` + `device_cgroup_rules` actually give the sidecar a hotplugged `/dev/ttyACM*`?**
  Verified on the box at step 5, not assumed here.
- **mDNS from ESP-IDF.** `jbrain.local` needs the mDNS component and a `.local` resolver that
  works from the firmware; the fallback is the box's LAN IP in NVS, which costs a re-provision
  if the lease moves. A DHCP reservation is the cheap answer and needs the router, not the box.
- **Which transport (§4.1).** Step 4 deliberately uses plain HTTPS + SSE — the endpoints the PWA
  screen already exercises — because that is zero new server work and it produces the latency
  number the MQTT-versus-WebSocket decision needs. Decide after step 6, with data.
