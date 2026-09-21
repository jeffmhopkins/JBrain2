# Room endpoints — the box's face and ears on a small AMOLED satellite

> **Status:** In progress · **Last verified:** 2026-09-20 · **Waves:** W1🟢 W2◻ W3◻ W4◻ W5◻ W6◻ W7◻

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

#### 10.4h Why the first OTA never happened: one scheme (2026-09-20)

The instrument added in §10.4g answered it on its first firing:

```json
{"version": "0.2.1", "url": "http://hopkinsbrain.com/api/endpoint/firmware/bin",
 "principal": "device_key", "host": "hopkinsbrain.com"}
```

**`http://`.** `esp_https_ota` refuses a plain-HTTP URL outright, so every over-the-air
update failed the instant it was attempted — on the panel, where nothing on this box
could see it. From here it was a 200 and no download, forever.

The cause is honest and useless: `request.base_url` reports the scheme of the hop that
reached uvicorn. Caddy runs in **Cloudflare Tunnel mode**, where its own site address is
`http://<domain>` and TLS terminates at the edge, and uvicorn is not told to trust
`X-Forwarded-Proto` from a container address. So the framework told the truth about the
internal hop and the wrong thing about the world.

**The same value is written into a panel's NVS at flash time** as the address it calls
home on, so the consequence is not only a failed update: Elora's panel had been polling
the box over plain HTTP with its bearer token on the wire. Treat that key as exposed —
a re-flash issues a new `device_key` and revokes the old one, which is exactly the
remediation and exactly what re-flashing already did.

`_public_base` now returns https **unconditionally**.

The first fix honoured a declared `X-Forwarded-Proto`, on the reasoning that a proxy which
says so is telling the truth about itself. It is — **about its own hop.** In tunnel mode
Caddy takes the request from `cloudflared` over plain HTTP and accurately declares `http`,
while the panel's connection to Cloudflare's edge was TLS the whole time. So the header
re-emitted the broken url verbatim and the OTA stayed broken **through a deploy**, with the
evidence sitting in the very log line added to catch it. The same mistake as the original,
moved one hop out: trusting a local observation to describe a remote fact.

**The test that existed asserted the url ended in `/endpoint/firmware/bin`** — true of the
broken value. A suffix is not an address, and four tests now pin the scheme on both halves
(the manifest, and what a flash writes into NVS), each confirmed to fail against what
shipped.

One thing this does NOT fix: the panel is reaching the box at the **public** hostname
rather than `https://jbrain.local`, so its traffic leaves the LAN and comes back through
the tunnel. `_panel_base` exists to prevent precisely that and did not apply, which means
either `JBRAIN_LAN_ADDR` is unset on this box or Caddy's internal root is not readable at
the mount. Worth its own look; it costs latency rather than correctness.

#### 10.4i The loop closed (2026-09-20)

Read off the panel's own console, through the debug route added the same hour:

```
I (287) boot: Loaded app from partition at offset 0x1a0000
I (317) app_init: App version:      0.2.1
I (397) endpoint: jbrain room endpoint, version 0.2.1
I (637) wifi:connected with bleepbloop, rssi: -60
I (2217) net: got 192.168.1.40
I (2717) endpoint: up to date at 0.2.1
```

**`0x1a0000` is `ota_0`.** The unit was USB-flashed into `factory` at `0x20000`; nothing but
`esp_https_ota` writes that other offset. So the image it is running arrived **over Wi-Fi**,
and W1's whole reason for existing is now demonstrated rather than argued: a panel on a
wall takes a firmware change from a `version.txt` bump with no cable.

Boot to "up to date" is **2.7 seconds**, all of it in the log above: Wi-Fi, DHCP, a TLS
fetch of the manifest, and the version comparison.

**A reading error worth recording, because it nearly caused a wrong fix.** Twice after the
scheme fix landed, this session concluded the OTA was still failing "on the panel side" —
from the ABSENCE of an `endpoint.image_served` line. The download had already happened; the
line was written by a container that `docker compose up -d` then replaced, and container
recreation discards the log. Absence of a log line is not evidence when the log itself is
younger than the event. The panel's own console settled it in one call, which is the
argument for §10.4g in one sentence.

**Still outstanding: the cleartext poll.** The panel's NVS holds the `http://` base it was
given at flash time, so its manifest requests still carry the bearer token in the clear.
Only a re-flash rewrites NVS — and that re-flash issues a new `device_key` and revokes the
old one, so the remediation and the fix are the same action.

#### 10.4j The panel was on the wrong side of the house (2026-09-20)

A panel three metres from the box was routing every request out to Cloudflare and back.
`_panel_base` exists to prevent exactly that, and its LAN branch needs two things: an
address, and Caddy's internal root to pin. Nothing said which was missing, so a diagnostic
was added first (`GET /api/debug/endpoint/address`). It answered in one call:

```json
{"lan_addr": "https://jbrain.local",
 "ca_error": "[Errno 13] Permission denied: /data/caddy/caddy/pki/authorities/local/root.crt",
 "ca_parent_listable": false,
 "panel_base": "https://hopkinsbrain.com/api"}
```

The LAN site was configured all along. **Caddy mints its CA as root, into a directory that
also holds the CA private key, so it is not world-traversable — correctly.** The api runs
as `appuser` (uid 1000) and cannot get to it. `_lan_ca` catches the `OSError` and returns
`""`, which is *also* what a box with no LAN site returns, so the fallback was silent and
indistinguishable from the normal state.

The fix is the one the owner proposed and the one `LOCAL_ACCESS.md` already tells a human
to do by hand: **copy the root out.** `deploy/proxy-publish-ca.sh` publishes it to
`/data/lan-root.crt` (mode 0644, written to a temp name and renamed), and `_lan_ca` prefers
that path with the original as a fallback for a proxy image that predates it.

Three details that are the whole design:

- **The root, never the key.** A root certificate is public by construction — every device
  that trusts this box already has it. The alternative considered was loosening the mode on
  the directory, which trades a real secret for a convenience.
- **A loop, not a one-shot.** Caddy mints the CA lazily, when it first serves the `tls
  internal` site — after the entrypoint has `exec`'d. There is no moment at startup when
  the file is reliably present, so the publisher runs in the background and re-copies.
- **A half-written file must never be pinned.** `_lan_ca` requires `BEGIN CERTIFICATE`
  rather than merely a readable file: a truncated root fails every handshake forever, on a
  device with no cable attached to it.

Verified viable before building: `firmware/sdkconfig` carries
`CONFIG_LWIP_DNS_SUPPORT_MDNS_QUERIES=y`, so a panel's ordinary resolver handles
`jbrain.local` over mDNS. Without that, pointing a panel at the LAN name would have been
worse than the hairpin it fixes.

#### 10.4k Flashing without a phone, and the secret that costs (2026-09-20)

The over-the-air loop was proven twice — once retroactively, once **watched**: the box
served 0.2.2, the panel fetched the manifest at 16:37:13, downloaded 985,440 bytes at
16:37:15, and checked in from `ota_1` at 16:37:29. Slot AND version moved together, which a
re-read of the same image cannot do.

That closes updates. It does not close **recovery**: a panel that stops working is fixed by
a re-flash, and a re-flash needed the owner at the PWA with the Wi-Fi password retyped —
the errand CLAUDE.md #10 exists to remove, on the one surface where it had quietly come
back.

So `POST /api/debug/endpoint/flash`, sharing `endpoint.build_flash` with the PWA's route.
One assembly for both, because a second copy would be a second place to forget the CA
pairing rule, and that failure mode is a panel that joins Wi-Fi perfectly and is then
silent forever.

**The cost, stated plainly: this is the first secret this surface keeps.** A flash needs the
Wi-Fi password, and nothing else here is stored — the device token is minted per request,
the CA is read per request. So the network is written to `app.settings` (owner-only RLS),
and **only when the owner ticks the box that says so**. Starting to store someone's Wi-Fi
password should be something they did, not something that began happening.

Two rules follow from where the caller is:

- **The password never comes from the request.** `PanelFlashIn` has nowhere to put one,
  because a password passed over the debug surface would live in a transcript forever. A
  box with no remembered network refuses and names the one action that fixes it.
- **Two panels visible refuses to guess.** A flash rotates the unit's identity; doing that
  to the wrong twin's panel by inference is worse than doing nothing.

#### 10.4l PSRAM, over the air (2026-09-20)

The first change W1 was built to make safe. `firmware/README.md` had said it in advance —
display, touch, audio and PSRAM are absent *on purpose*, and "those arrive over the air, onto
a unit that has already proved it can take an update" — and the unit has now taken three.

It is the gate for everything visual rather than a nice-to-have. One RGB565 framebuffer at
368×448 is **322 KB**; this chip reports **332 KB** of internal RAM free at boot, with Wi-Fi
and TLS still to feed. The display cannot exist without it.

Two properties made it safe to attempt remotely, and both were checked before pushing:

- **Only the app image changes.** `bootloader.bin` is byte-identical to 0.2.2, because PSRAM
  is brought up by the app. Had the bootloader moved, an OTA could not have carried this at
  all — OTA rewrites an app slot and nothing else — and the change would have needed a cable.
- **`CONFIG_SPIRAM_IGNORE_NOTFOUND=y`.** The default on a wrong mode (Quad silicon behind an
  Octal config) is to panic in early boot, which is a boot loop on a device with no cable.
  This degrades it to "came up without PSRAM".

That second one creates a gap the rollback gate cannot close, and it is worth naming: a panel
that comes up *without* PSRAM still reaches the box and still marks itself good. Nothing about
the safety net would notice. So the boot log reports the size explicitly, and **that reading —
not "it booted" — is what says this worked.**

#### 10.4m The display, and four things guessing would have got wrong (2026-09-20)

W3's first milestone: the panel draws. Colour bars, nothing else — same discipline as PSRAM,
so that a failure has one variable.

The product documentation publishes neither the pin map nor the init sequence, so the source
is the vendor's own `13_display_colorbar` example (`waveshareteam/ESP32-S3-Touch-AMOLED-1.8`),
read rather than inferred. Four things in it would not have been guessed:

1. **This board ships in two revisions and does not say which it is.** V1 is SH8601 + FT3168;
   V2 is CO5300 + CST820. The plan's §1 hardware table names V1 because that is what was
   ordered — but the units arrived later, and nothing in the boot log distinguishes them.
2. **One driver covers both.** The vendor drives V1's SH8601 with the **CO5300** driver; the
   two controllers take the same command set here. The only difference that reaches the
   screen is a **16-pixel column offset** on V2.
3. **So the revision is probed at runtime**, the way the vendor probes it: V2's touch
   controller answers at I2C `0x15` and V1's does not. Getting it wrong costs a shifted
   image, not a blank one — which is why an unanswerable probe assumes V1 and continues.
4. **There is no reset line.** `reset_gpio_num = GPIO_NUM_NC`; the init sequence does the work.

**The one deliberate departure from the vendor's code: nothing here aborts.** The example is
`ESP_ERROR_CHECK` throughout, which is right for a bench demo and wrong for this. An abort is
a boot loop; a boot loop on a unit with no cable is a screwdriver; and a panel that cannot
reach the box cannot be sent the fix. A dark screen is a bad day, an unreachable unit is the
end of the line — so every failure returns and `app_main` continues to Wi-Fi regardless.

**A correction to §10.4l.** That entry said the display "cannot exist without" PSRAM. Too
strong: this draws in 16-row stripes, 11.5 KB of DMA-capable internal RAM, and needs no PSRAM
at all. PSRAM is what the **animated** face needs — a full 368×448 framebuffer is 322 KB, and
double-buffering it is 644 KB. The claim was right about W4 and wrong about W3.

**Reproducibility survived a fetched dependency.** `esp_lcd_co5300` comes from the component
registry and resolved to 2.2.0 rather than the vendor's pinned 2.1.0, which is exactly how a
byte-for-byte CI check dies quietly. `firmware/dependencies.lock` is committed and pins every
component by version *and content hash*; `managed_components/` is ignored. Verified by
building the same tree at two different paths for identical SHA-256s, as with 0.2.1.

#### 10.4n The panel drew, and then went dark (2026-09-20)

0.2.4 worked: eight colour bars, correct order, and the V2 gap offset right — the runtime
revision probe earned itself immediately, because **this board is V2 (CO5300/CST820)** and
§1's hardware table says V1. That table records what was *ordered*; the units arrived later.
Hardcoding V1 from it would have produced a 16-pixel column shift — an image that looks
almost right, which is the worst kind of thing to chase.

Then the owner looked at it and the screen was black. A power cycle brought the bars back.

**The firmware was never in trouble.** `endpoint.manifest_served` at 19:35:02, 19:35:21 (the
OTA reboot) and 19:50:23 — a 15-minute cadence, exactly on schedule. No crash, no reboot
loop. The display went dark while everything else kept working, which rules out the whole
family of explanations worth reaching for first.

Two candidates fit, and they need opposite fixes:

- **The controller dropped display-on.** We draw once and never touch it again; if the panel
  loses that state, SPI is still alive and a repaint brings it back.
- **The AXP2101 cut the display rail.** The board has a PMU this firmware has never spoken
  to, brightness is at `0x51 = 0xFF` (full), and a full-brightness AMOLED plus Wi-Fi TX
  spikes is exactly the load a PMU protects itself against. If the rail is off, no amount of
  SPI helps.

0.2.5 is the experiment rather than a fix, and deliberately does **not** touch brightness —
lowering it would confound the two. It repaints every ten seconds, **alternating** the bar
order, because that makes the answer readable from across a desk: flipping means the panel is
being driven, frozen means the writes are landing nowhere, dark means neither. The log line
says only that the bus accepted the write, which is a different question from whether
anything appeared, and it says so.

#### 10.4o It flips: the panel will not hold a still image (2026-09-20)

The experiment read clean. **The bars alternate every ten seconds**, so the writes are
reaching the glass and the AXP2101 is not cutting anything — the power-rail hypothesis is
dead, and with it the need to teach this firmware to speak to the PMU.

What remains is narrower and stranger: **this panel does not hold a static image
indefinitely.** Drawn once, it was dark within minutes; written to every ten seconds, it
stays lit. The exact mechanism inside the CO5300 is not pinned.

**That is deliberately left unpinned**, and the reason is worth stating rather than
discovering later as a silence: the thing this panel is for is an animated face, which
redraws continuously by construction. Chasing the controller's idle behaviour buys nothing
the product will ever notice, and the cost of being wrong is a repaint that was already
going to happen.

**But it is a live trap for anything that stops moving**, and several planned features do:

- **Quiet hours** (§"Carried into the plan") cut luminance before bed. A dimmed *still*
  face is a face that vanishes.
- A sleeping pet, a clock, a notification card left up — every one of them is a static
  screen, and every one of them would go dark and read as a dead device.

So the rule this establishes: **something must keep writing to the panel, always.** A low
frame rate is fine; zero is not. Whatever W3's render loop becomes, its idle path is a slow
refresh rather than a stop — and the brightness change that quiet hours needs is a `0x51`
write, not a pause.

Brightness itself is still at `0xFF` and still wrong for a bedroom; it was held back only so
it could not be mistaken for the fix, and that reason has now expired.

#### 10.4p The robot, and a tap that changes it (2026-09-20)

The rest pose, on the glass: head, eyes with pupils and catchlights, antenna, smile, torso,
chest plate, arms and legs. A tap cycles the eleven shipped colours.

**Every number came from the mock, not from taste.** `pet-face.html`'s canvas is 368×448 —
the panel exactly — so its coordinates need no mapping at all. That is what building the mock
at true geometry bought: the design was measured against this screen before the screen
existed, and copying is more faithful than re-deriving.

**What made this checkable before it touched hardware:** `face.c` has no ESP dependencies —
`face.h`, `math.h`, `string.h` — so it compiles on the host. A twenty-line harness renders the
framebuffer to a PNG, which means the geometry was *looked at* rather than flashed and hoped
for. It caught the real defect immediately: omitting the limbs left ~100 px of dead black
below the torso and a figure that sat high. The legs reach y≈396; they are load-bearing for
the composition, not decoration. **Keep that harness** — W4's rig is where a wrong sign in a
tween costs an OTA cycle to see.

Three decisions worth recording:

- **Touch is not a touch driver.** One register (`0x02`, the finger count) on the CST820,
  because the whole screen is the only target the measurement allows — a coordinate would be
  a component dependency and a rotation convention to get wrong, in service of a distinction
  this design does not make.
- **It reports the EDGE, never the hold.** 4–5 year olds produce ordinary taps lasting up to
  4.2 seconds; a level-triggered read would change colour eleven times while he looked at it.
- **The face runs in its own task.** A frame rate must never be able to delay an OTA check —
  the update path outranks the picture, always.

The render loop has a **500 ms floor** rather than drawing once, which is §10.4o's rule
honoured rather than worked around: nothing moves yet, so the floor is the product
requirement, and W4's animation simply raises it.

Still absent and deliberately so: the emotions, the tweening, the gags. This is the thing the
rig will animate, not a substitute for it.

#### 10.4q A beep on every tap (2026-09-20)

0.2.7 gives the tap a sound. The ES8311 is brought up through Espressif's `esp_codec_dev`
rather than the vendor's BSP — the BSP would also want to own the display and the touch
controller this firmware already drives.

**The pin map had a contradiction in it, and getting it wrong is silent.** The vendor's
Arduino `pin_config.h` carries both `I2S_DO_IO 8`/`I2S_DI_IO 10` **and** `DOPIN 10`/`DIPIN 8`
— the same two pins named from opposite ends of the link. A coin flip there gives no speaker
*and* no microphone, with no error from either side: I2S clocks out happily into a pin
nothing is listening on. Resolved against the BSP component
(`waveshare/esp32_s3_touch_amoled_1_8` v2.0.3, pulled from the registry and read):
`BSP_I2S_DOUT = GPIO_NUM_8` is ESP→codec, `BSP_I2S_DSIN = GPIO_NUM_10` is codec→ESP, with
MCLK 16, BCLK 9, WS 45 and the amplifier enable on 46. The microphone work in 0.2.8 depends
on the same answer, which is why it was resolved by authority rather than by trying one.

**Volume is a safety limit, not a preference.** The vendor example ships 90/100 for V2
hardware. This is a 29 mm object a four-year-old will hold to his ear, and ASTM F963 /
EN 71-1 cap close-to-ear toys at 65 dB(A) (§"Carried into the plan"). Nothing available here
can measure decibels, so it starts at 55 and the owner's ear is the instrument. It moves up
only against a measurement, never against "it seems quiet".

**The tone is shaped, not switched on and off.** 880 Hz for 90 ms with a raised-cosine fade
over the first and last fifth. A square-edged tone clicks at both ends, and the click is the
loudest thing in it — which is precisely the part a dB(A) cap exists to govern. It is built
once at start-up and written from the face task between frames, so a tap costs an I2S write
rather than two thousand calls to `sinf`.

**The I2C bus now has one owner** (`i2c_bus.c`). This is a bug that had not happened yet: the
display's revision probe created a bus and deleted it, touch created its own, and the codec
would have been the third. Deleting after probing is what kept that working, and it worked by
accident — `i2c_new_master_bus` fails with a laconic `ESP_ERR_INVALID_STATE` on a port that
already has one, on a device whose only symptom is that one peripheral silently does nothing.

Silence is logged rather than inferred: a beep that never comes could be the codec, the
amplifier pin, the volume or a tap that was never registered, and only the first of those is
visible from the firmware. `audio_start` returning false logs *"no codec — taps will be
silent"* and the face carries on.

`bootloader.bin` and `partition-table.bin` are byte-identical to 0.2.6, so this is a pure app
OTA and a rollback lands on the same bootloader.

#### 10.4s The black screen was probably the debug console, not the panel (2026-09-20)

0.2.7 reached the panel, the beep worked, and the robot went black. The first explanation
written here was that §10.4o had misread its own experiment — that the variable was *changed*
content rather than writes, since 0.2.7 redrew identical frames every 500 ms and a tap revived
it. A bob was built and a rule was rewritten.

**Then the owner supplied the observation that beats it:** unplugging and replugging brings
the robot straight back, and the blackouts line up with *me reading the panel's console*.

That has a mechanism, and it is in our own tooling. `deploy/endpoint/monitor.py` is careful on
the way in — DTR and RTS are set false **before** `open()`, precisely so listening does not
restart anything — and then hands `close()` to the Linux tty layer, which drops both lines. On
the S3's USB Serial/JTAG those lines are not bookkeeping: **RTS is reset and DTR is the boot
pin.** The wrong transition on the way out leaves the chip in the ROM bootloader with the
application never started. From the room that is a black screen that stays black until someone
pulls the cable — which is precisely the reported symptom, including the part my hypothesis
could not explain at all: that it *came back by itself* (a later read resetting it into the
app) and that a power cycle fixes it.

**This also puts §10.4n and §10.4o in doubt**, because the console tool has existed since
#1439 and every "the panel went dark" observation since has been made in a session where I was
reading it. The 0.2.5 bars appearing to flip may mean only that the panel was reset into a
working app between glances. **The "panel will not hold a still image" finding is suspect and
is no longer being built on.**

Three things follow:

- **The bob was pulled from 0.2.8 before merge.** Not because it is bad — an animated pet wants
  it, and W4 will have something like it — but because shipping it now would destroy the
  experiment. If the panel then stayed lit, the bob would get the credit that belongs to *not
  being poked*. One change at a time cuts both ways: it also means not adding one.
- **The tool is fixed** so a watch cannot leave a panel dead: it now ends by pulsing the chip
  back into its application. That costs an unasked-for reboot, which this module's own
  docstring calls a surprise worth avoiding, and it is still the right trade against a panel
  that is dark until someone finds it.
- **The experiment is to leave it alone.** A static face, nobody reading the console, and see
  whether it stays lit. Costs nothing but patience, and it is the only clean read available.

The general lesson is the expensive one: **the instrument was changing what it measured.** Every
observation of the display in this session was taken through a tool that can halt the CPU, and
none of the reasoning accounted for it. A diagnosis built on such observations is worth less
than the confidence it was delivered with — and it was delivered with a great deal.

#### 10.4t The panel blanks with nobody on its console (2026-09-20)

§10.4s retracted the content-change finding in favour of the owner's hypothesis: that the
blackouts were **my own console reads** leaving the chip in the ROM bootloader. That
hypothesis had a real mechanism and a real symptom match, and the tool was genuinely broken,
so it was the right call on the evidence available.

**It is not the whole story.** After 0.2.8 deployed, the panel went black again during a
stretch when the only thing being read was the *box's* API log — nothing touched the panel's
serial port. So the display blanks on its own.

One thing to be careful about, because it reads the wrong way round: when a console read
"brings the robot back", **that is a reboot, not a revival.** `panel-console` resets by
default. It is the same restoration a power cycle gives and says nothing about the console
being a cure.

So the ledger, honestly:

| observation | what it rules on |
| --- | --- |
| blanks with nobody on the serial port | the tooling bug is not sufficient to explain it |
| a reboot restores it | consistent with everything; discriminates nothing |
| a tap restores it | the content changed |
| identical frames every 500 ms do not prevent it | write rate is not the variable |

The last two are what §10.4s originally reasoned from, and they are back in play. **The
tooling bug was real and is fixed; it was just not the cause of this.** Two true things were
competing for one symptom.

0.2.9 is therefore the bob, restored unchanged from the version that was pulled: the figure
moves ±5 px on a four-second integer triangle, so consecutive frames are unequal by
construction. It is the treatment for the surviving hypothesis and a clean experiment either
way:

- **stays lit** → consecutive frames must differ; §10.4o's rule was the weaker reading of its
  own evidence, as §10.4s first argued.
- **still blanks** → content is not the variable either, and the next instrument is the
  CO5300's own power-mode register (`0x0A`), read while the screen is dark. That says
  directly whether the controller still believes the display is on, which is the §10.4n
  question that was inferred past rather than answered.

**The process lesson is not "trust the first theory".** It is that the first theory was
argued from two clean controls and then abandoned on a single correlation, and the
correlation turned out to be a second real bug sitting on top of the first. Neither
retraction nor re-assertion was free: what was missing both times was an experiment that
could separate them, which is what 0.2.9 is.

#### 10.4u The bob did not fix it either, so stop guessing and read the part (2026-09-20)

0.2.9 reached the panel at 23:51:30, marked itself good nineteen seconds later, and the robot
went black anyway — while bobbing, so every frame differed from the one before it.

**That kills the content-change hypothesis**, which was §10.4s's, restated in §10.4t, and the
thing 0.2.9 existed to test. Two explanations have now been built from the symptom alone and
both were wrong:

| version | hypothesis | result |
| --- | --- | --- |
| 0.2.7 | the panel must be WRITTEN to (§10.4o) | identical frames every 500 ms → dark |
| 0.2.9 | consecutive frames must DIFFER (§10.4s) | a bob on every frame → dark |

A third guess is not worth an OTA cycle, and the pattern is the point: every one of these was
reasoned from the outside, from what the symptom looked like, when the CO5300 has been able to
answer the question directly the whole time.

**0.2.10 asks it.** `RDDPM` (0x0A) is the MIPI DCS register that reports what the controller
believes about its own state, and `RDDISBV` (0x52) reports the brightness it thinks it is
running. Read every ten seconds, logged, read-only. The answer partitions cleanly:

| reading, while the screen is dark | conclusion |
| --- | --- |
| display bit SET, brightness high | not the controller — the OLED rail, or the AXP2101 this firmware has never spoken to |
| display bit CLEAR | the controller dropped display-on; re-issuing `0x29` is the fix |
| idle-mode bit SET | the part has an idle mode nobody asked for, which would explain every observation including why a tap helps |
| the read itself fails | the panel is not answering at all, which is its own answer |

The probe gives up after three consecutive failures rather than logging forever: it is an
instrument, and an instrument that floods the console is one more thing hiding the evidence.

**§10.4n is the part to be sorry about.** It listed exactly two candidates — the controller
dropping display-on, and the AXP2101 cutting the rail — said plainly that they *need opposite
fixes*, and then settled the question by watching bars flip rather than by asking the chip.
Everything since has been an elaboration of that shortcut. The bob stays in regardless: an
animated pet wants it, and it costs nothing.

#### 10.4v The version on the glass, and a hold that means it (2026-09-21)

Two operator affordances the owner asked for, both of which this session's confusion argued
for independently.

**The running version, top-left, in a 5x7 font.** Until now the only ways to know what a panel
was running were to ask the box what it last *served* — which is not the same question, and
§10.4t is the section where I got exactly that wrong — or to cable it up and read its console,
which resets it. Neither is available to someone standing in the room looking at the thing.

The corner is not arbitrary and was not guessed: the head spans x 76..292 and starts at y 60,
and the antenna ball is centred, so the top-left is the one region the robot never occupies.
Checked by compositing the label over the real `face_draw` on the host rather than by reading
coordinates, because reading coordinates is how the limbs went missing in §10.4p.

**The font is deliberately tiny** — digits, `.`, `v`, `-`, space. Hand-drawing twenty-six more
glyphs for words nothing renders yet would be inventory, not work. An unknown character draws
as a blank of the right width, so a wrong string is visibly wrong rather than silently short.

**A five-second hold reboots, which re-pulls firmware.** The number is the owner's and it is
well chosen, for a reason worth writing down: §10.4p measured 4-5 year olds producing
*ordinary* taps lasting up to **4.2 seconds**, so five is the first threshold that sits outside
a child's accidental press at all. That margin is 0.8 s and it is thin — which is precisely why
the hold is not silent. From 1.5 s an amber bar grows across the top edge, full width at the
moment it reboots, so the gesture announces itself in time to let go.

A reboot *is* the firmware re-check: `main.c` asks the box before its first sleep, so the
gesture doubles as "go and get the update now" without any new protocol.

The ordering inside the render loop is load-bearing and was wrong first: the hold is measured
**before** the frame is composed, so `held` describes the frame about to be drawn, and the
restart happens **after** the blit, so the full-width cue actually reaches the glass. Written
the other way round, the `dirty` flag was a dead store cleared at the top of the next
iteration, and a reboot with no warning is indistinguishable from the fault §10.4u is chasing.

#### 10.4w The probe returned zeros, and the console cannot see the fault (2026-09-21)

0.2.10's probe reported, while the panel was dark:

```
panel 0x0A=0x00 [display OFF, sleep IN, idle off, booster OFF] 0x52=0x00
```

Read literally that is a diagnosis — the controller dropped display-on — and it was very
nearly taken as one. **It is not data.** `0x52` is the brightness readback and `0x51` is
written `0xFF` in the init sequence; a working read would report `0xFF` there. Both registers
returning `0x00` means the CO5300 is not answering reads over QSPI at all.

**An instrument that fails by returning a plausible wrong answer is worse than one that
errors**, and this one is worse still because the give-up logic never fired: the call returned
`ESP_OK` with a zeroed buffer. The guard was written for the wrong failure mode. The check that
caught it was having a register whose value was already known — which is the only reason this
did not become the third fix built on nothing.

**And the console cannot observe the fault at all.** That log came from a FRESH BOOT despite
`--no-reset`: opening the USB CDC port resets the S3 whatever pyserial is told, because the
kernel asserts DTR before those settings apply. §10.4s fixed the *exit* path so a watch cannot
strand a panel; it did not make the *entry* path non-destructive, and it cannot. **Every
console read in this investigation has been of a panel that had just restarted, never of the
dark state.** That is worth stating plainly because it retroactively weakens every boot log
quoted in §10.4n onwards.

So both instruments are gone: reads return zeros, and looking resets. 0.2.12 stops trying to
observe and re-asserts instead — `0x29` (display on) and `0x51` (brightness) every thirty
seconds — with the experiment read off the glass rather than out of a log:

- **stays lit** → the controller was dropping display-on or brightness, and this is the fix
  rather than merely the diagnosis.
- **still blanks** → nothing the controller is told matters, which points at the OLED rail and
  the AXP2101 this firmware has never spoken to. That is then the last candidate standing from
  §10.4n's original pair, and the next work is the PMU.

Brightness is re-sent at `0xFF` to match the init sequence exactly. It is still wrong for a
bedroom and still has to come down, but not in the release that is testing one thing.

**A soft reset does not fix it; a power cycle does.** The owner found this by using the new
five-second hold: the amber cue filled, the panel rebooted, and it came back **black**. The
gesture works exactly as designed and the display does not come back with it.

That is the most informative thing observed all night, because of what it excludes. The driver
already issues `SWRESET` on this board — `reset_gpio_num` is `GPIO_NUM_NC`, and
`panel_co5300_reset` takes the software path when there is no reset GPIO — and the whole init
sequence re-runs on every boot. So the ESP32 does everything it does from cold, and the panel
still stays dark. **Whatever holds the display off therefore lives outside the ESP32**, in a
part that a power cycle clears and `esp_restart()` does not.

The candidates are the chips on the I2C bus, and here is the embarrassing part: **nobody has
ever confirmed which chips those are.** §10.4n named the AXP2101 and ruled it out by
inference; the vendor BSP does not mention a PMU at all, and it *does* expose a TCA9554 IO
expander that this firmware has never touched. Both `BSP_LCD_RST` and `BSP_LCD_BACKLIGHT` are
`GPIO_NUM_NC`, so neither is a line we are failing to drive.

0.2.12 therefore also logs an **I2C scan at startup** — twelve lines that answer what has been
assumed twice. If 0x34 acknowledges there is an AXP2101 and §10.4n's second candidate is alive;
if 0x20 acknowledges the expander is real and worth reading; if neither, the search moves off
this bus entirely.

**If 0.2.12 does not settle it, the next move is not another firmware guess — it is
telemetry.** The panel should report its own state to the box over HTTP on a cadence, because
every other channel either resets it or lies, and a panel that can only be diagnosed with a
cable is the thing this whole design exists to avoid (CLAUDE.md #10).

#### 10.4x The bus scan: the AXP2101 is real (2026-09-21)

```
I (955) i2c: i2c devices: 0x15 0x18 0x20 0x34 0x51 0x6b
```

`0x15` CST820 touch, `0x18` ES8311 codec, `0x51` RTC, `0x6b` IMU — and the two that matter:
**`0x20`, the TCA9554 IO expander, and `0x34`, the AXP2101 PMU.** Both real, neither ever
spoken to by this firmware.

§10.4n named the AXP2101 as one of exactly two candidates at 0.2.4, said plainly that the two
**need opposite fixes**, and then ruled it out by watching colour bars flip. It exists. It is
the last candidate standing, and it is precisely the class of part that holds state across
`esp_restart()` and is cleared by removing power — which is the signature the owner produced
with the five-second hold: reboot leaves the screen black, pulling the plug does not.

The same log carried 0.2.11's last probe before it updated — `0x0A=0x00`, still zeros —
confirming §10.4w's reading that the register path is dead rather than the display being off.

**The remaining problem was that the dark state cannot be observed.** Reads over QSPI return
zeros; opening the console resets the chip before anything can be seen. Every instrument so far
has either lied or destroyed what it measured.

**0.2.13 records instead of reading.** Six AXP2101 registers — two status bytes, the chip id,
and the three enable registers that decide which rails are actually up — sampled every ten
seconds into `RTC_NOINIT_ATTR` memory. That memory survives `esp_restart()` and is cleared only
by a power cycle, and a magic word distinguishes "survived a restart" from "powered up with
whatever was in the SRAM". At boot the ring is logged oldest-first and then cleared.

**Which makes the five-second hold the capture trigger.** It was built as a maintenance
gesture; it turns out to be the shutter. The owner sees a dark screen, holds for five seconds,
and the next boot log contains the two minutes of PMU state leading up to the fault. **That is
the first instrument in this investigation that does not destroy what it measures**, and it
exists only because the hold happened to be a soft reset rather than a power cycle.

What the reading will settle: if `dcdc_en` or an `ldo_en` bit is clear while the screen is dark
and set while it is lit, the PMU is cutting a rail and §10.4n's second candidate wins after
four releases. If the registers are identical in both states, the PMU is innocent, and the
remaining suspects are the TCA9554 at `0x20` and the OLED supply itself.

#### 10.4y Telemetry, because the cable was the diagnosis (2026-09-21)

The owner asked to move the panel off the box's USB port onto a plain charger, and wanted to
know first whether updates were really over the air. They are, and it is worth having the
evidence rather than the assurance: the firmware's only update path is `esp_https_ota` against
`cfg->api`, there is no USB update code in it at all, the panel's boot log shows
`net: got 192.168.1.40` then `update offered: 0.2.11 -> 0.2.12`, and the box logs the image as
an HTTP GET. `deploy/endpoint/ports.py` only *lists* `/sys/class/tty` names and never opens a
port, so enumeration was never resetting anything either.

**But unplugging would have cost the diagnosis.** §10.4x's PMU capture is read out of a boot
log, and a boot log needs the console, and the console needs the cable. The capture would have
survived the move and become unreadable — the worst of both.

So 0.2.14 gives the panel a voice: `POST /api/endpoint/telemetry`, authenticated with the same
`device_key` the manifest poll uses, carrying version, uptime, reset reason, free heap and
PSRAM, and **the PMU history that survived the last restart**. Sent once at boot before
anything else touches the ring, and again on every fifteen-minute cycle.

**This is the third channel, and the first one that works.** Register reads over QSPI return
zeros that read exactly like a diagnosis (§10.4w). Opening the console resets the chip, so
every log captured in this investigation was of a freshly-booted panel rather than of the
fault. Telemetry neither lies nor disturbs: it is the panel's own account, arriving over the
network it already uses, landing in a log the owner can already read.

Nothing is stored. These are a panel's claims about itself, read by a human looking at a log,
and a table would be a schema to migrate every time the question changes — which is the point,
because what a panel needs to report changes with whatever is being chased that week.

**The five-second hold now closes the loop without a cable.** See a dark screen, hold five
seconds, and the panel reboots, reconnects and posts the two minutes of PMU state that preceded
the fault to the box. That is the whole diagnostic path with no terminal and no USB host, which
is what CLAUDE.md #10 has been asking for since the beginning — and it took the display bug to
force it, because a panel that can only be diagnosed with a cable is a bench unit, not a room
endpoint.

One consequence to state plainly: with USB gone, **OTA plus rollback is the only recovery
path.** That is exactly what the first image was built around — a frozen factory app, and a
rollback gated on reaching the box — so it should hold. It does mean a bad image is recovered
by the bootloader rather than by the owner.

#### 10.4z Off the cable, and the volume is settled (2026-09-21)

Two things closed on the owner's word, both of which had been left explicitly open.

**Volume: 70/100 is right.** §10.4q set 55 against the vendor's 90 because nothing in the
session could measure decibels, and said plainly that it would move only against a
measurement and never against "it seems quiet". The owner reported 0.2.7 as *a little bit
quiet*, 0.2.8 raised it to 70, and 0.2.11's report is *audio is good*. That is the
measurement, it is the only kind available here, and the number is no longer a guess. Still
well under the vendor's default, and the raised-cosine envelope is unchanged — the envelope
is what a dB(A) cap is really about, not the peak.

**The panel now runs on a plain USB charger.** Moved off the box's port at ~01:40:31 — the
`reset_reason: "power"` in its own telemetry — and it came back up, joined Wi-Fi, fetched the
manifest at 01:40:33 and reported itself at 01:40:35. No USB host, no console, no cable to
anything but power.

That is §10's premise finally cashed rather than asserted: *the box's USB port is available
for the first flash only.* Everything since has arrived over the air, and now everything
**about** the panel leaves over the air too.

`pmu_history: []` in that first report is correct rather than a failure. A power cycle clears
the RTC ring, so nothing survived — and "we were not looking" is a different fact from "the
PMU was fine", which is why the empty case is reportable rather than an error.

**Which changes how the outstanding display fault gets caught.** The capture survives a soft
reset and not a power cycle, so the gesture matters:

> Find it dark → **hold five seconds** → it reboots, reconnects, and posts the two minutes
> before the fault. Do not pull the plug; that erases exactly the evidence.

Still open: the display fault itself, where the leading model is now that **the panel freezes
— stops accepting frames — and the retained image fades to black**, on the strength of the
owner seeing a stale `v0.2.11` label while 0.2.12 was running. And the microphone, written and
parked at §10.4r's design, waiting on a screen that reliably stays lit, because its whole test
is a level meter drawn on that screen.

#### 10.4aa The microphone, always on, down the left edge (2026-09-21)

The design parked at §10.4r was press-and-hold to record, release to play back. The owner
asked for the simpler thing — *microphone active all the time with a meter on the left side* —
and it is the better test, not merely the cheaper one.

**Why always-on beats record-then-play.** Playback confounds two devices: silence at the end
could be the ADC, the PGA, the I2S receive direction, the speaker or the volume, and the test
cannot say which. A live meter isolates the capture path on its own, needs no gesture to
operate, and is legible to a four-year-old as well as to whoever is debugging it. §10.4r's
level indicator was always the diagnostic half; the playback was the part that added
ambiguity.

**The read is the clock.** One chunk per frame, sized to the frame period, so capture drains
exactly as fast as the I2S DMA fills it. Consuming any slower shows a meter falling further
behind the room every second — a fault that looks like bad calibration and is actually a
backlog. It also means the mic, not `vTaskDelay`, paces the render loop whenever the codec is
up, and the loop falls back to the timer when it is not.

**The left edge is free by construction**, not by luck: the head spans x 76..292 and the arms
reach x 104 at their widest, so a bar at x 4..16 never touches the robot. Checked by
compositing it over the real `face_draw` on the host before flashing anything, which is the
same harness that caught the missing limbs in §10.4p.

**The peak also goes out in telemetry**, which is the first real use of the channel built in
§10.4y. The meter answers *is the microphone working* for whoever is standing in front of the
panel; `mic_peak` answers it for whoever is not — and the panel is now on a charger in another
room, so that is most of the time. Zero across several reports while someone is talking near
it means the capture path is dead.

`mic_peak` defaults to 0 on the API rather than being required, because a telemetry route that
422s on a field older firmware does not send would silence exactly the panel that needs
attention.

Gain stays at 30 dB — the ES8311 quantises to 6 dB steps up to 42 — and `METER_FULL` is 12000
rather than full scale, because a child at arm's length lands nowhere near 32767. Both are
first guesses, and now both are measurable: the reported peaks are what will move them,
exactly as the owner's ear moved the volume.

#### 10.4ab The knobs become settings, and why not in `app.settings` (2026-09-21)

Microphone gain, speaker volume and display brightness were compile-time constants. Every one
of them cost a build, a CI run, a deploy and an OTA to change — which is why the volume took
two full rounds to settle on 70, and why the gain and the brightness have never been tuned at
all. Brightness is still `0xFF` and still wrong for a bedroom, and quiet hours needs to change
it at runtime by design rather than by release.

**The obvious implementation is wrong, and this repo already contains the argument.**
`app.settings` is where the remembered Wi-Fi lives and would have been the natural home. It is
gated on a bare `app.is_owner()` and it holds the Gmail client secret, the Moltbook bearer key,
the autonomy switch and the global kill. A panel authenticates as a `device_key`, and
`device_context()` refuses *on purpose* to launder a device into owner scope so a stolen panel
key cannot read everything. Using that table would have meant either laundering the scope or
resting on "the route only returns three fields" — and `0178_settings_deny_jmolt` exists
precisely to say that a route's shape is a code-review convention, not a mechanism.

So `app.endpoint_settings`: one row, owner-writable, `device_key`-readable, holding three
numbers that are worth nothing to a thief.

**The panel policy is `FOR SELECT`**, which makes read-only structural rather than a matter of
which routes happen to exist. A panel that could write its own volume would be a device on a
child's wall able to raise the level in its own ear, which is the one thing §10.4q's 65 dB(A)
reasoning exists to prevent.

**The isolation test found something the design did not predict.** A panel's `UPDATE` is not
*rejected* — the row is simply invisible to it, so Postgres matches nothing and reports success
with zero rows. The first version of the test expected an exception and therefore passed a
write that had in fact been denied, proving nothing either way. It now asserts the effect: zero
rows touched, and the owner's value still there afterwards. The denial is silent, and that is
worth knowing before someone reads a clean log as a clean attempt.

**The ceilings live in the API, not in a CHECK constraint.** A rejected write gives a 500 and
no guidance; a clamp turns a typo into a safe value and logs what it did next to what was
asked. `VOLUME_MAX` is 85 — above the confirmed-good 70, below the vendor's 90 — so a slipped
digit cannot put 100 into a speaker held to a four-year-old's ear, while going louder stays a
deliberate commit against a measurement. `MIC_GAIN_MAX` is 42 because the ES8311's PGA
truncates above it. `BRIGHTNESS_MIN` is 10 because a panel at zero is indistinguishable from
the fault §10.4u is still chasing.

**Applied at boot and on every cycle, from the OTA task rather than the render loop**, so a
slow or unreachable box can never stall a frame. Each field is read independently, so a box
running older code that omits one still delivers the others. And the five-second hold reboots,
so it doubles as *apply this now* — tuning is seconds instead of a release.

The re-assert in §10.4w now re-sends the *current* brightness rather than the constant, or a
setting would be quietly undone thirty seconds after it was made.

#### 10.4ac The IMU, reported before it is acted on (2026-09-21)

The owner suggested using the board's gyroscope, with keeping the robot facing up as the easy
test. It is a good test and a real feature — a panel can be mounted, hung or knocked into any
orientation — with one correction worth making explicit.

**It wants the accelerometer, not the gyroscope.** Gravity is a constant 1 g pointing down, and
which way that lands on the chip's axes is the entire answer. A gyroscope measures rotation
*rate*: it says the panel is turning, never which way is up, and integrating it drifts. So the
QMI8658's gyro half stays powered down — it costs current and answers a question nothing here
is asking.

**The axes are not assumed, and that is why this release does not flip anything.** Neither the
vendor BSP nor the sample repo mentions this part at all, so nothing on this box knows how the
chip is glued down relative to the screen. Guessing the sign gives a robot that is upside down
permanently, which looks exactly like a bug and would be indistinguishable from one — and this
session has already spent four releases on a fault whose instruments were lying. So 0.2.17
brings the part up, checks `WHO_AM_I` before writing a single control register, and reports raw
counts in telemetry. The flip lands once the numbers say which way is down.

Reporting **raw** counts rather than a derived orientation is the same discipline: a firmware
that reported "upright" would be asserting the very thing being measured. All zeros means the
part did not answer, which is its own reading.

`WHO_AM_I` is checked first because something acknowledging at 0x6b is not the same as it being
the part this code knows how to configure, and writing control registers into whatever else
might be there is how a working I2C bus stops working — with the display, the touch controller
and the codec all sharing it.

Configured for ±4 g at about 59 Hz. The range matters, since 1 g must sit well inside it with
headroom for the knock of being put down; the rate does not, because this is sampled roughly
once a second alongside the rest of the telemetry.

**The flip itself will be a 180° reversal of the framebuffer**, not a driver mirror: a 180°
rotation of a row-major buffer is exactly reversing it, which costs one pass over 165k pixels,
cannot interact with the V2 panel's 16-pixel column gap, and flips the version label and the
microphone meter along with the robot — which is what "facing up" has to mean. Ninety degrees
is not available: the panel is 368×448 and a quarter turn does not fit it.

#### 10.4ae Gravity is on X (2026-09-21)

0.2.18 shipped the flip on an assumed axis, at the owner's call, with the raw counts going out
in telemetry. The first report answered it in one cycle:

```
accel: [-7637, 381, 530]
```

Upright, gravity is **-0.93 g on X**. `ay` — the axis 0.2.18 guessed — reads 381, which sits
deep inside the ±4000 hysteresis band, so the flip would never have triggered at all. Not
inverted: inert.

0.2.19 is `ay` → `ax` and nothing else.

**Shipping the guess was the right call and this is why.** The alternative was a reporting
release followed by an acting release: two cycles either way, except the guess had a chance of
being right and left the mechanism already deployed and exercised. The cost of being wrong was
one line, exactly as predicted — and the failure mode was even cheaper than the one anticipated,
because an axis reading near zero does nothing rather than doing the wrong thing.

The general point is the one §10.4ad opened: caution is priced per mistake. Reporting raw
counts rather than a derived orientation is what made this a one-line answer instead of a
guess about a guess.

#### 10.4af A reading is worthless without a named orientation (2026-09-21)

0.2.19 moved the flip from `ay` to `ax` on the strength of one telemetry line,
`[-7637, 381, 530]`, and shipped the sign backwards. The axis was right; the sign was read off
a pose nobody had named. The panel was lying on its charger and that reading was, it turns out,
the panel INVERTED.

The owner then held it in a stated orientation — charging port right, robot's head up — and
asked for a reading at that moment. `[8446, 78, -563]`. Two things fell out at once: `ay` and
`az` are both near zero in a known-upright pose, which confirms X as the vertical axis beyond
the earlier inference, and **right way up is positive `ax`**, which is the opposite of what
0.2.19 assumed.

**The lesson is not "measure twice".** 0.2.18 reported raw counts precisely so the axis could
be settled by data, and it was. The gap was that a *magnitude* identifies an axis from any pose,
while a *sign* means nothing until someone says which way the thing was facing. The first
reading could settle the first question and not the second, and 0.2.19 used it for both.

The fix was one comparison, as §10.4ae predicted the cost would be — but it was the second
one-line fix for the same feature, and the first was avoidable by asking "in what orientation?"
before reading a sign off a number.

Getting the answer also took a round trip that did not need to exist: telemetry is on a
fifteen-minute cycle, so the live question "what does it read right now" was answered by having
the owner hold the panel for five seconds to force a reboot and an immediate report. That works,
and it is a sign that a panel should be able to answer a question sooner than its next
scheduled one.

#### 10.4ag He leans before he flips (2026-09-21)

The owner asked for the robot to fall left or right, proportionally, as the panel is tilted —
"until we are flipping". It makes the flip feel like the end of something rather than a jump
cut, and it turns a binary into a continuous readout of the same sensor.

The figure slides horizontally in proportion to the sideways component of gravity: full lean at
a little over a quarter of a gravity, because tilting a panel that far is a deliberate act and
anything gentler should stay proportional rather than pinned. Smoothed, because the
accelerometer is noisy at rest and a figure twitching while the panel sits still reads as
broken rather than alive.

**±60 px is what the composition allows, and it was measured rather than estimated.** The head
is 216 px on a 368 px panel, so the arithmetic says 76 px of slack each side. The host harness
was asked instead: at full lean the figure spans 16..231 and 136..351, clearing both edges by
16 px and stopping just short of the microphone meter at x 4..15. Worth recording that the
rendered image *looked* clipped to me and the measurement said otherwise — the eye is not a
measuring instrument, which is the whole reason that harness exists (§10.4p).

**The sign DOES need a case when inverted**, and the paragraph that used to sit here said the
opposite. It claimed two negations cancel: the panel's rotation negates `ay`, and `flip_frame`
negates the drawn offset. **The second is not a negation the viewer sees.** `flip_frame`
reverses the framebuffer and the panel is then physically rotated 180° in the viewer's hands —
*those* two cancel, so the viewer reads framebuffer coordinates directly in both orientations.
Only the accelerometer's sign actually flips.

The owner found it in one sentence: *"tilt is backwards when right side up, correct when upside
down and flipped"* — which is precisely the signature of one uncompensated negation rather than
two cancelling ones. 0.2.22 takes the tilt in viewer terms explicitly: the chip turns over with
the panel, the rendered image does not.

The mistake was reasoning about a rotation without asking where the observer was standing. It
is the same shape as §10.4af, where a sign was read off a pose nobody had named — both times
the arithmetic was fine and the frame of reference was missing.

Tilting now redraws at the poll rate rather than waiting out the idle floor, but only once the
lean has moved more than two pixels — otherwise every frame would be a full 322 KB blit for a
pixel of accelerometer noise.

#### 10.4ad The meter was showing one frame in five, and the robot flips (2026-09-21)

**The sluggish meter was a bug, not a limit.** The microphone is sampled 25 times a second and
`level` was recomputed every one of them — but the bar was only drawn when the WHOLE face was,
which is five times a second. Four readings in five were captured, folded into the reported
peak, and never shown. The meter was not lagging the room; it was showing one frame in five of
it.

The fix is that the meter gets its own blit. A strip 12 px wide is **8.8 KB against 322 KB for
the frame**, so it can be pushed on every capture while the face keeps its slower cadence.
Partial updates were always available; nothing needed to get faster, one thing needed to stop
being coupled to everything else.

**Fast attack, slow decay** on top of that. A raw per-chunk peak pushed 25 times a second is
honest and looks like noise; rising instantly and falling over about a second is what makes it
read as a meter. Only the fall is smoothed, so a loud moment still registers on the frame it
happened in.

**The flip ships without the reporting round §10.4ac argued for**, on the owner's call:
*"if it is upside down we can fix that — that's the whole point."* That is the right trade and
the reasoning is worth keeping. §10.4ac's caution was borrowed from the display fault, where a
wrong guess cost hours because the instruments lied and the symptom was ambiguous. Here the
symptom is a robot standing on its head: unmistakable, visible in a second, and one sign flip
to correct. Caution is priced per-mistake, and this mistake is cheap.

So `ay` is the assumption, with hysteresis at about half a gravity because a panel lying near
flat has almost nothing on that axis and a bare sign test would flip back and forth on noise.
Telemetry still carries the raw counts, so if it arrives upside down the fix is one comparison
and nothing else.

**The flip is a reversal of the framebuffer**, which is exactly what a 180° rotation of a
row-major buffer is — one pass, no resampling, and it takes the version label and the meter
with it, which is what "facing up" has to mean. The meter's own partial blit mirrors both its
content and its window, so the bar stays on the viewer's left rather than travelling to the
other side of the screen. The two agree to the pixel: the full frame's x ∈ [4,16) maps to
[352,364), which is exactly the window the strip blit uses.

Ninety degrees remains unavailable — 368×448 does not fit a quarter turn — so this is upright
or inverted, which is what a panel that has been hung, mounted or knocked over actually needs.

#### 10.4ah The rig starts: a blink and a flinch (2026-09-21)

W4's ~17 tweened floats begin with two, and the parameters move into a `face_state_t` struct
rather than a fifth positional argument. The caller owns the tweening; `face.c` knows only how
to draw one instant of it, which is what keeps it ESP-free and renderable on a host.

**Blink is the cheapest of the seventeen and does the most.** One number, no new geometry, and
it is the difference between a face and a picture of a face. **Jittered**, because a blink
exactly every four seconds is a metronome — the regularity is what gives away a machine, and
the irregularity is most of the effect.

Two details the harness caught before the panel did:

- **A shut eye is a lid LINE, not an absent eye.** Drawing nothing for the closed frames reads
  as the face breaking for a moment rather than as a blink.
- **The pupil is clamped to the lid height.** Without it a half-closed eye shows a bar of pupil
  spilling past the white, which reads as damage. That only appears in the middle of the
  transition, which is exactly the frame nobody renders when checking by eye.

**The flinch is the interaction the design settled** (§"Interaction, settled"): touch means *"I
am paying attention to you"*, expressed as a sub-100 ms movement toward the finger. Here it is a
dip of 18 px with wide eyes and shrunken pupils, decaying over about half a second — startled,
then recovering, which is what makes a poke feel answered rather than merely registered. Both
extremes were rendered and their bounds measured: rest spans y 13..414, poked 31..432, so the
dip does not push the legs off a 448-px panel.

**Animating means every poll is a frame.** A blink at the 200 ms idle floor would be one frame
long and read as a glitch, so any frame with a flinch or a part-closed eye marks itself dirty
and the loop draws at its poll rate instead. The floor is for a face that is holding still.

One consequence to watch: the microphone read paces that loop, so a burst of animation slows
consumption slightly and the meter can lag by a fraction of a second during a poke. The I2S DMA
ring bounds it and it catches up at rest. If that becomes visible, the fix is the same one the
meter already uses — draw less than the whole frame — not a faster loop.

Still to come from the design: the six emotions as lid geometry, asymmetry for curious and
silly, the gag structure (the hold on a **bewildered** face is the joke), and W4b's
weighted-random variant pools with per-variant cooldowns.

#### 10.4ai It was never the display. The panel panics. (2026-09-21)

The owner reported a black screen and held for five seconds, and the boot report carried the
answer that six releases of display theories had not:

```
reset_reason: "panic"   03:21:46
```

**The panel crashed.** Not a controller dropping display-on, not a rail being cut, not frames
failing to land — a firmware panic. And it closes the loop on the one observation that never
fitted a display fault: **a panic is a soft reset**, and §10.4x established that a soft reset
leaves the screen dark where a power cycle does not. Crash, reboot, black until someone pulls
the plug. Every symptom follows from that, including "it came back by itself" (a later crash or
reset that happened to re-init cleanly).

Everything from §10.4n onward was looking at the wrong subsystem. The display evidence was real
and the reasoning was mostly sound; it was all downstream of a crash nobody could see, because
the only channel that would have shown `reset_reason` was telemetry, and telemetry did not exist
until §10.4y — which was itself built because the owner wanted the cable gone.

**A second finding, and it is a bug I wrote.** `pmu_history` has been empty in every report, and
it is not because nothing survived: `display_start()` calls `pmu_report_history()`, which
*cleared the ring*, long before the first telemetry POST could read it. The capture built in
§10.4x to survive a soft reset was being thrown away at boot, and the empty array looked exactly
like the honest "cold boot, nothing survived" case it was designed to report. Clearing now
happens after the history has left the box.

**The suspect for the panic is the render task's stack.** It was created with 4096 bytes when it
drew a static colour pattern and nothing else. It now runs, every frame: an I2S capture, an
accelerometer read, font rendering, PMU sampling, float tweening, a full-frame composition and
two LCD blits. A fault that arrives as the work grows is the signature of a stack running out.

0.2.24 raises it to 8192 **and stops guessing**: `uxTaskGetStackHighWaterMark` is sampled every
frame and the smallest headroom seen goes out in telemetry as `stack_free`. A shrinking number is
a panic that has not happened yet — which is the whole point, because the panel is on a charger
in another room and reading its console resets it.

If `stack_free` stays comfortable and the panics continue, the stack is exonerated and the next
candidate is the memory the new work allocates rather than the stack it runs on.

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
