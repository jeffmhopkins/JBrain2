# Room endpoints — the box's face and ears on a small AMOLED satellite

> **Status:** In progress · **Last verified:** 2026-09-20 · **Waves:** W1🟡 W2◻ W3◻ W4◻ W5◻ W6◻ W7◻

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

> **Superseded 2026-09-19.** The owner assigned the units: **one per twin**, both eventually in
> their rooms. So the bench unit is a *phase*, not a unit — one panel is flashed and exercised
> first, then joins the other in the field — and after that there is no sacrificial board and no
> cable in either bedroom. Everything above still holds while a unit is on the bench; what
> changes is that the safety net in §10.3 stops being insurance and becomes the only way back.
> Staggering still applies in its new form: **never push an OTA to the second unit until the
> first has taken it and come back.**

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
| **W2** | **Protocol + device identity.** N-endpoint addressing on the shipped `device_key` model, broker config defaulting to empty so that half of the feature is absent when unset (the `sdr_url` pattern). **Note `endpoint_url` went the other way** — it defaults to the running service (`pysandbox`'s pattern), because gating the flasher behind a profile meant an `.env` edit on the host to enable a PWA-only feature; see §10.4b. | No new auth model. |
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
| Build | GitHub Actions, `espressif/idf` image | `firmware.bin` — **no credentials, no token, no certificate**. Identical for both units, so it is committed to `firmware/dist/` and travels with the repo (§10.4b). |
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

**Label the units physically, now — one per twin.** Both end up in bedrooms (§4.4, superseded),
so "bench" is the phase one of them is in, not a unit that stays behind. Whichever is flashed
first takes the cable and the mistakes; the second is only ever flashed once the first has
taken an OTA and come back. That ordering is the whole of what is left of the bench/field
split, and step 7 is where it pays.

### 10.4a What W1 has actually built (2026-09-19)

`firmware/` — the ESP-IDF project, building clean against **v5.5.5** for esp32s3, with the
§10.3 safety net in place from the first image: rollback enabled and gated on reaching the box,
a frozen `factory` app, and the partition table above, ending at exactly 16 M. The built image
is **894 K against the 1.5 M factory slot (42% free)**, which confirms the recovery app — which
does strictly less — fits. `.github/workflows/firmware.yml` builds and publishes it; ESP-IDF is
~3.5 GB, so `scripts/dev-setup.sh` deliberately does not install it and `scripts/firmware-setup.sh`
is the opt-in local installer.

Two errors were caught by compiling rather than by a board: inline comments in `partitions.csv`
are parsed as the Flags column, and `esp_http_client_get_user_data` takes an out-parameter.
Neither would have been visible without the toolchain, which is the argument for step 4 existing
before step 5.

Still to build for the first flash: the `deploy/endpoint/` sidecar, the Ops button, and the box
side of the one endpoint the firmware needs —

```
GET {api}/endpoint/firmware        Authorization: Bearer <device token>
→ 200 {"version": "0.1.0", "url": "…/endpoint/firmware/bin"}
```

— where `version` is compared verbatim against `firmware/version.txt`, so **bumping that file is
what makes a unit update.**

### 10.4b The box side, built (2026-09-19)

The flash path exists, so a panel plugged into the box's USB port is now reachable from
**Endpoints** (its own launcher tile) and from nowhere else — which is the point, since the owner has no
terminal and the debug console's `host.read` scope reports memory and processes, never
device nodes.

**`deploy/endpoint/`** — the flasher sidecar, **stock stack rather than profile-gated**, on
a `panel` network declared `internal: true` so the container that writes a bootloader has no
route off the box.

The profile was the first design and it was wrong. `sdr` is opt-in because most boxes have
no dongle; this box has panels. Gating the flasher would have meant editing
`/opt/jbrain2/.env` on the host to switch on a feature reachable only through the PWA — a
terminal step to remove a terminal step, which is rule 10 read backwards. With the URL
defaulting to the running service (the `pysandbox` pattern), **Ops → Update is the whole
install**, and the cost is ~30 MB idle when nothing is plugged in. It never fetches firmware; the api hands
over the images, which is what keeps a GitHub credential off this box entirely. It keeps
nothing between requests: the Wi-Fi password and device token are written under a temporary
directory that is removed whether the flash succeeds or fails.

It is **not** the `sdr` device mapping, and §10.6's open question is now answered in code
rather than in prose: `sdr` maps `/dev/bus/usb`, which works for a raw libusb dongle, but
the ESP32-S3 enumerates as a kernel CDC-ACM tty that does not live under that path and does
not exist until a panel is plugged in. So: a `/dev` mount plus `device_cgroup_rules` for
the tty character majors (166 CDC-ACM, 188 USB-serial) — narrower than `privileged`, and
able to admit a hotplugged device. **Whether it works is still proven on the box, not here.**

**Generic image, personalised at flash time**, as §10.1 designed. The owner uploads the CI
artifact once; the api stores the images in `BlobStore` and the version in `app.settings`
(owner-only RLS, and a new key there is a constant rather than a migration — so no new
table and no new isolation test). At flash time it mints a **fresh `device_key`** on the
shipped substrate, reads **Caddy's own root certificate** from a new read-only `caddy_data`
mount, and sends both into NVS. That last part closes finding **C** in code: the owner never
runs the `docker cp` that `../runbooks/LOCAL_ACCESS.md` otherwise requires.

A re-flash therefore issues a **new** identity and revokes the old one. That is correct
rather than incidental: re-flashing is how a panel is handed over or recovered, and the key
it used to hold should stop working at that moment.

`GET /api/endpoint/firmware` is the manifest the firmware polls, and the one route a panel
in a child's bedroom can reach. It is owner-or-`device_key`, and returns a version and a URL
and nothing else.

**It was not actually reachable by a panel until 2026-09-20 (§10.4f).**

**What still needs the owner: nothing but the tap.** The firmware is already on the box.

This took three passes, each removing something the previous one had argued for.

1. **Download the CI artifact and upload it.** Defended on the grounds that the alternative
   meant a GitHub credential on the box and a path by which the box fetches and then
   executes code from the internet. Both halves were wrong: this repository is public, so
   nothing needs a credential; and the box already `git fetch`es this repo and runs what it
   gets on every Ops → Update, so "fetches and executes from the internet" describes an
   update, not a new risk — and a firmware image is the *less* dangerous of the two, since
   it runs on the panel rather than on the box. Removed 2026-09-19.
2. **`firmware.yml` cuts a release; `POST /endpoint/firmware/sync` pulls it** into the
   `BlobStore`, verifying every asset against the release's own `SHA256SUMS` first. Correct
   in principle, and it failed on the first real flash: `httpx.ConnectError: [Errno -5] No
   address associated with hostname`, surfaced to the owner as `Request failed: 500` while
   they stood there holding the board. Removed 2026-09-20.
3. **The committed images are the distribution.** `firmware/dist/` holds the built set, the
   api mounts `firmware/` read-only off `src`, and a flash reads the bytes straight off the
   checkout the box already keeps current.

The third is not a workaround for the second's outage — it is what the second should have
been. `update-inner.sh` already pulls this whole repository from `main` and rebuilds the
stack from it on every Ops → Update, over a path proven to work on this box because
*everything else on the box arrives through it*. A second channel, reaching three hosts the
box otherwise never talks to, existed only to deliver a ~1 MB file that the first channel
was already fetching the source of. So the firmware a panel can be flashed with is simply
the firmware in the checkout the box is running: **no release, no CDN, no credential, no
network at all at flash time, and no button to press before the Flash button works.**

The price is ~1 MB of built images in git per firmware version, and a rule that a firmware
change is not finished until `scripts/firmware-dist.sh` has run. `firmware.yml` enforces the
rule exactly — it rebuilds and **fails the PR unless `dist/` is byte-for-byte what the
source produces**, which `CONFIG_APP_REPRODUCIBLE_BUILD=y` makes possible at all (ESP-IDF
otherwise stamps the build date and absolute build paths into every image; verified here by
building the same tree at two different paths and getting identical SHA-256s).

Two things came out of the rewrite that the release path had been hiding:

- `GET /endpoint/firmware/bin` — the URL the manifest advertises to a panel — **had no
  implementation**. Every OTA a panel attempted would have 404'd; nothing had noticed
  because no panel had yet got far enough to attempt one.
- The checksum verification survives, now against `dist/SHA256SUMS` at flash time. It no
  longer guards a hostile network — it guards a half-finished update or a stale `dist/`
  beside a newer `version.txt`, and a truncated image that flashes is still worse than one
  that refuses.

#### 10.4f The first panel to boot found the door shut

Within minutes of the first successful flash, the api log filled with
`GET /api/endpoint/firmware → 401`.

Both panel-facing routes were written against `PrincipalDep` and then checked
`principal.kind in ("owner", "device_key")`. `PrincipalDep` resolves the owner's **session
cookie** as a session token and reads no `Authorization` header at all, so a panel's bearer
key never reached that check — **it was dead code guarding a door that was already shut**.
The firmware had always sent the credential correctly (`ota.c`, and `firmware/README.md`
documented the contract); the backend simply never implemented its half.

Nothing caught it because nothing could: the tests exercised the manifest through an
authenticated owner client, which takes the cookie branch and never touches the bearer path.
A panel had to boot, join Wi-Fi and knock before the gap was observable — which is exactly
what W1 exists to make possible, and the first thing it found.

It matters more than a 401 usually does. Reaching that route is the panel's **health
signal**, the thing the firmware's rollback gate waits on before marking an image good, and
it is also the **OTA download path**. A panel that cannot reach it can never be updated
again over the air — the single property this whole wave was built to guarantee.

The fix is `PanelDep` (`api/deps.py`): owner cookie, or `Authorization: Bearer <device_key>`
resolved through the same kind-filtered lookup the OwnTracks and MQTT paths use. Deliberately
a **separate dependency** rather than teaching `current_principal` to read bearer tokens —
that would hand a device key every cookie-gated route in the app, and a panel is a device on
a child's wall. A test asserts the key opens the manifest and the image **and nothing else**,
and the positive tests were confirmed to fail against the shipped code.

The near-miss worth naming: the first version of those tests passed while the bug was still
present, because the test client still carried the owner cookie. Clearing it is what made
them honest.

### 10.4c What the assembled unit showed (2026-09-19)

A photograph of a panel in the hand, plugged into the box, settled three things no
datasheet had:

1. **It works out of the box.** The factory demo runs — Wi-Fi glyph, battery, clock, four
   app icons, page dots — so the step-0 smoke test is passed on at least one unit, and a
   dead screen after flashing is now unambiguous rather than a question.
2. **The case rounds the display into a squircle.** The AMOLED is a 368×448 rectangle, but
   the enclosure hides its corners: anything drawn there is invisible to whoever is holding
   it. The PWA preview drew the full rectangle, which is wrong in the same family as the
   CSS-`mm` size bug — it shows pixels that cannot be seen, and invites a design that loses
   content on the desk. `pet/draw.ts` now clips to the case shape and leaves the corners
   **transparent** rather than black: on an AMOLED black and off are identical, so a black
   corner would say "this part of the display is dark" when the truth is "there is no
   display here". Verified in a real browser by sampling the rendered canvas — all four
   corners `alpha 0`, edges and centre opaque.
3. **Portrait, with USB-C on the right.** Which the enclosure work will care about, and
   which decides how a unit sits on a shelf.

The corner radius is measured off the photograph rather than a datasheet, so
`CASE_CORNER_FRACTION` is approximate and deliberately slightly generous: over-masking
hides a corner that might exist, under-masking invites a design that loses content.

### 10.4d First contact, and why the next step is blind (2026-09-19)

A panel plugged into the running box appeared in the PWA on the first try — see §10.6.

**What that does not prove, and the problem it creates.** This firmware draws nothing: no
display, no touch, no audio, deliberately (§10.3). So a *successfully* flashed panel shows
a **black screen**, and is visually indistinguishable from a bricked one. The bring-up goes
blind at exactly the point where being blind stops being cheap.

The evidence a flash worked is on the **box**, not on the panel. The firmware's first act
after joining Wi-Fi is `GET /api/endpoint/firmware` carrying its own device token, so that
single request proves the entire chain: Wi-Fi joined, the box's certificate validated, the
token accepted, the manifest parsed. It lands in the api's logs, which the owner can read
through the debug console without a terminal.

**This argues for a serial monitor in the flasher next.** `deploy/endpoint` already owns the
port and already streams a log to the PWA; reading the panel's own boot output over the same
USB connection would turn a blind bring-up into a legible one, and it is the cheapest
feature left in this wave. Until it exists, the manifest request is the only signal.

**Built 2026-09-20 (§10.4g), after it cost us exactly what this paragraph predicted.**

#### 10.4g The monitor, and the bill that came due for not having it (2026-09-20)

The first OTA did not happen. The box served 0.2.1, the panel ran 0.2.0, and the panel
polled the manifest every 15 minutes, got `200`, and never requested the image.

From the box, that is indistinguishable from a panel that is correctly up to date — the
same two log lines either way, with completely different fixes. The reason was a line the
panel was printing to a console nobody could read. §10.4d called this exactly, and the
first real debugging session after it was written is the one that got stuck on it.

Two instruments were added, in the order that cost least:

- **`endpoint.manifest_served`** (#1435) — the version and the **url** the box hands over,
  because the url is *derived* from the request's own base address and so is the field most
  able to be quietly wrong. A manifest a panel can read pointing at an image it cannot
  fetch fails inside `esp_https_ota`, where nothing on the box can see it.
- **The console monitor** (`deploy/endpoint/monitor.py`) — the panel's own output, over the
  same USB it was flashed through. No second cable and no UART header: the S3's native USB
  carries both, and `CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG` (ESP-IDF's default,
  now pinned) is what puts the console on it.

**Resetting is the feature, not a side effect to avoid.** A panel checks for firmware every
15 minutes, so watching a running one means waiting up to a quarter of an hour for the
interesting line. A reset makes the whole boot — Wi-Fi, TLS, manifest, version comparison,
OTA attempt — happen in the first few seconds. So it is offered explicitly and defaults to
off in the API: an unasked-for reset of a panel on a child's wall is not a surprise this
surface should have. The PWA defaults it *on*, because someone who opened a console window
is debugging.

**The port is exclusive and a flash outranks a watch.** `POST /flash` asks any monitor on
that port to let go and waits for it, before the response status is committed so a refusal
is still a status code. Getting this backwards would mean the owner pressing Flash and
being refused by their own debugging tool.

### 10.4e Two bugs found before the first flash (2026-09-19)

Both surfaced from the owner asking a plain question — *does this firmware connect to
Wi-Fi, and can we test it?* — which is worth recording, because neither had a symptom that
would have been diagnosable after the fact. Each produces a panel that joins Wi-Fi
perfectly and is then silent forever.

**1. `ca` was required by the firmware and conditional in the flasher.** `cfg.c` listed it
as a mandatory NVS key; the api wrote it only when a Caddy root was readable. A box without
a LAN site would therefore flash a panel that fails `cfg_load` on boot, logs *"no
provisioning"* and stops — before touching the radio. `ca` is now optional.

**2. The panel was told whatever address the owner's browser was on.** `_public_base()`
returns `request.base_url`, so flashing from the PWA at a public hostname handed the panel
that URL — **while also handing it the box's internal CA root**, which is the only thing
the firmware trusted (`CONFIG_MBEDTLS_CERTIFICATE_BUNDLE=n`). Every handshake against a
publicly-signed certificate would fail, forever, with no symptom: the panel joins Wi-Fi,
fetches nothing, marks itself unhealthy and rolls back. On this box, reached at its public
hostname, that was the likely outcome of the very first flash.

The address and the certificate now travel together or not at all (`_panel_base`):

- LAN address configured **and** its root readable → `https://jbrain.local/api` + that root
  pinned. This is the intended path: a panel is on the same LAN, pinning one certificate
  beats trusting ~150 public CAs, and it keeps the pet off the internet round trip.
- otherwise → the owner's address + **no** root, validated against the public bundle the
  firmware now carries.

`JBRAIN_LAN_ADDR` already existed on the host for Caddy's local site but was only passed to
the `proxy` service; the api now receives it too, which is what lets it answer "where should
a panel look for me" with something better than "wherever you happened to be".

Cost: the CA bundle grows the image from 894 K to 985 K, still 37% free in the factory slot.
Firmware is **0.2.0**; three backend tests pin the pairing and were confirmed to fail
against the shipped behaviour. Adding the bundle also needed `mbedtls` in the component's
`REQUIRES` — the third defect in this wave that only compiling would find.

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

- ~~**Does `/dev` + `device_cgroup_rules` actually give the sidecar a hotplugged
  `/dev/ttyACM*`?**~~ **ANSWERED YES on the box, 2026-09-19.** A panel plugged into the box
  while the containers were already running appeared in the PWA as
  `/dev/ttyACM0 — Espressif ESP32-S3 (native USB) (panel)`. So the cgroup grant admits a
  hotplugged character device, the sysfs walk attributes it to vendor `303a`, and the whole
  chain — kernel, container, api, phone — works on the first try. The host-side half was
  confirmed independently through the debug console's USB scan: `303a:1001 Espressif USB
  JTAG/serial debug unit`, `cdc_acm` bound. This was the largest unknown in W1 and it is
  closed.

- **mDNS from ESP-IDF.** `jbrain.local` needs the mDNS component and a `.local` resolver that
  works from the firmware; the fallback is the box's LAN IP in NVS, which costs a re-provision
  if the lease moves. A DHCP reservation is the cheap answer and needs the router, not the box.
- **Which transport (§4.1).** Step 4 deliberately uses plain HTTPS + SSE — the endpoints the PWA
  screen already exercises — because that is zero new server work and it produces the latency
  number the MQTT-versus-WebSocket decision needs. Decide after step 6, with data.
