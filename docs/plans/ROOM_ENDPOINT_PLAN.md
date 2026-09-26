# Room endpoints — the box's face and ears on a small AMOLED satellite

> **Status:** In progress · **Last verified:** 2026-09-26 · **Waves:** W1🟢 W2◻ W3◻ W4🟢 W4b🟢 W5◻ W6◻ W7◻

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
gesture; it turns out to be the shutter. (From 0.2.30 it is three short taps and then the hold
— §10.4ap.) The owner sees a dark screen, performs it,
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

> **The title is wrong and §10.4am says how.** The panic was real and is fixed. The black
> screen is a SEPARATE fault: on 0.2.27, with no panic recorded, a dark panel still beeped when
> tapped — the render task alive, blitting, and re-asserting display-on into a screen that
> stayed off. Everything below about the panic stands. "It was never the display" does not.

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

#### 10.4aj Both suspects dead, and a breadcrumb instead of a backtrace (2026-09-21)

The capture from §10.4ai worked on its first real panic, and killed both hypotheses at once.

```
reset_reason: "panic"   stack_free: 5532
pmu_history: 8 × "20 15 4a 0f ff 01"
```

**The PMU is exonerated.** Eight samples spanning the blackout, byte-identical: `DCDC_EN=0x0f`,
`LDO_EN=0xff/0x01`, every rail up and unchanging. `chip_id = 0x4a` confirms it really is an
AXP2101. §10.4n's second candidate is closed by measurement rather than by watching colour bars,
which is what should have happened at 0.2.4.

**The stack is exonerated too, and the number that proves it is the one added to test the
guess.** 5532 bytes of headroom out of 8192 means the render task's deepest use was about 2.7 KB
— it was never close, even at the original 4096. §10.4ai called an overflowing stack "the
candidate that fits a fault which arrived as the work grew"; that reasoning was plausible and
wrong, and it took one instrumented release to find out instead of a chain of inference.

So: a panic, reproducible within about a minute of boot, with memory and power both healthy.

**The backtrace is unreachable and that is not fixable.** The panic handler prints it to the USB
console, which this panel does not have — it is on a charger in another room, which is the state
the whole design has been driving toward — and opening that console resets the chip anyway
(§10.4w). No partition exists for a core dump either: the table is permanent and has no
`coredump` entry, so adding one means a USB reflash of both units.

**0.2.25 writes breadcrumbs instead.** The render loop stores its current stage in
`RTC_NOINIT_ATTR` memory, which survives the reset a panic performs, and the next boot reports
the last stage reached. The stages, in order: 1 loop top, 2 touch, 3 beep, 4 stack probe,
5 IMU, 6 `face_draw`, 7 label, 8 flip, 9 full blit, 10 microphone read, 11 meter blit,
12 panel re-assert, 13 PMU sample — 14, brightness, added by §10.4ak, and 15, codec levels,
by §10.4al. The same list lives
next to `PHASE()` in `display.c`; a number whose stage nobody can name is worth nothing.

That is not a line number. It is the difference between "somewhere in the firmware" and "in the
I2S read", and it costs one store per stage. A cold boot reports −1 rather than claiming stage
0, because "nothing survived" and "it died at the top of the loop" are different facts — the
same distinction §10.4x drew for the PMU ring, and the same one §10.4ai found had been
accidentally erased.

#### 10.4ak Two tasks, one SPI panel handle (2026-09-21)

**The panic has a mechanism, and it is a race this firmware wrote itself.**

`esp_lcd_panel_io_spi` is not thread-safe, and not in the mild sense that phrase usually
carries. Read `panel_io_spi_tx_param` in IDF v5.5.5: it acquires the SPI bus, reads
`num_trans_inflight`, drains every queued transfer with
`spi_device_get_trans_result(..., portMAX_DELAY)`, decrements the count once per drain, and
then `memset`s `trans_pool[0]` — the very slot `tx_color` fills from the other side. Two tasks
on one io handle can therefore

- drain each other's transfers, so one of them decrements `num_trans_inflight` past zero.
  It is a `size_t`, so that wraps to `SIZE_MAX` and the next drain loop waits on
  `portMAX_DELAY` for transactions that will never be queued — **holding the bus**; or
- `memset` a descriptor while DMA is still reading it.

**And there was exactly one cross-task caller.** `display_set_brightness()` wrote `0x51`
straight to the io handle, and `apply_settings()` calls it **from the main task**, at boot and
on every fifteen-minute cycle — while the face task drives the same handle at about 25 fps.
The box serves a brightness unconditionally (`endpoint_settings` ships `255`), so this fired on
every boot of every panel, with no setting ever having been changed. §10.4aj's "reproducible
within about a minute of boot" is the shape that produces: `reach_box` plus `apply_settings`
lands right there.

**0.2.26 gives the panel one owner.** `display_set_brightness()` now only records the value and
raises a flag; the render loop applies it, next to the re-assert that was already the only
other command writer. It is phase 14 in the breadcrumb map. `display_repaint()` went with it —
dead since §10.4u replaced probing with re-asserting, and a second cross-task entry point into
the same handle for anyone who called it.

**What this does not claim.** It is a mechanism, not yet a verdict. The black screens run back
to 0.2.4 and `apply_settings` only arrived at 0.2.23, so either the early ones have a different
cause or the panic was simply unobservable before telemetry (§10.4y) — and this session has
already spent two releases on a symptom with two true bugs competing for it. `crash_phase` is
what settles it: a face task dying in a blit (9, 11) or a command write (12, 14) fits this race;
a death in the I2S read (10) or the IMU (5) does not, and sends the search elsewhere.

> **Wrong, and §10.4al says why.** The breadcrumb records where the RENDER task is. A fault in
> the MAIN task — which is where both cross-task calls live — reports whatever stage the render
> loop was parked in, and it parks in stage 10. The first reading that came back was 10, and by
> this paragraph's rule that would have sent the search elsewhere. It should not have.

**Cost of the diagnosis: reading the driver.** Nothing about it needed the panel, a console or
the owner — the race is visible in sixty lines of `esp_lcd_panel_io_spi.c` and in `grep` for
who calls the panel from which task. It was available at 0.2.23 and went unlooked-for through
three releases of instrumenting the hardware instead.

#### 10.4al The first breadcrumb, and what it actually says (2026-09-21)

The panel was unplugged overnight and came back at 12:00. At 12:16 it panicked, and the box's
log is the clearest thing this investigation has produced:

```
12:15:31  endpoint.manifest_served                 GET /api/endpoint/firmware   200
12:15:31                                           GET /api/endpoint/settings   200
          ——— nothing ———
12:17:00  (reboot) manifest + settings again
12:17:07  telemetry  version 0.2.25  reset_reason "panic"  crash_phase 10
```

**The fifteen-minute cycle fetched its settings and then never posted its telemetry.** In
`app_main` those are adjacent:

```c
apply_settings(&cfg);   /* GET /settings, then audio_set_levels + display_set_brightness */
report(&cfg);           /* IMU read, JSON, POST /telemetry */
```

So the fault is bounded to the handful of lines between a `200` on `/settings` and a POST that
never came — and both of the calls in that window are the main task reaching into hardware the
render task owns. That is a far tighter bound than six releases of probing the display
produced, and it came out of an HTTP access log.

**`crash_phase: 10` is not the contradiction §10.4ak called it.** The breadcrumb records where
the RENDER task is, and the render task was not the one crashing. It parks in stage 10: the
capture blocks for a full 40 ms of every 40 ms frame, so stage 10 is most of the loop's wall
clock and is what any main-task fault will report. §10.4ak wrote the discriminator backwards —
it read the number as a crash site rather than as one task's position — and had the panel been
left to that rule, the first real reading would have been filed as exculpatory. The lesson is
the one §10.4w and §10.4x kept teaching: **an instrument that answers a different question than
the one asked is worse than one that stays silent.** The comment beside `PHASE()` now says what
the number means.

**0.2.26 fixed one of the two calls in that window; 0.2.27 fixes the other.**
`esp_codec_dev.c` has no lock of any kind — read, write, `set_out_vol` and `set_in_gain` all
walk straight into the device struct and the codec's I2C registers. The component's only mutex
is in `audio_codec_data_i2s.c` and guards the data path, which does nothing for a control write
arriving from another task mid-capture. So `audio_set_levels()` now records and the render loop
applies it, as stage 15 — the same shape as brightness, for the same reason, on the other chip.

**What is still not proven.** Which of the two calls actually panicked is unknown and may stay
unknown: they sat in the same window, on the same cadence, and after 0.2.27 neither runs
cross-task. If a `panic` returns on 0.2.27 the window bound is gone and the search starts again
with the phase number read correctly this time; if it does not, the honest statement is "both
races closed and the panic stopped", not "it was the codec".

#### 10.4am It beeped. Two faults, and an instrument that could never fire. (2026-09-21)

**The owner tapped a dark panel and it beeped.**

That one bit ends the conflation §10.4ai built. The panel was on 0.2.27, no panic had been
recorded since its 12:48 boot, and the five-second hold that followed reported
`reset_reason: "sw"` — so nothing had crashed. The render task was alive the whole time:
polling touch, playing a tone, drawing a frame every 40 ms, and re-asserting `0x29` and `0x51`
every thirty seconds into a screen that stayed black.

So there are **two faults**, and this session fixed the other one. The panic was real
(§10.4ak, §10.4al) and its two cross-task races are closed. The black screen is its own
problem and always was, and §10.4u's experiment has now answered itself by its own rule:

> *it still blanks -> nothing the controller is told matters, which points at the OLED rail and
> the AXP2101 this firmware has never spoken to.*

It still blanks, while being told continuously. Nothing the controller is told matters.

**And the instrument built to read that moment could never have fired.** `pmu_history` has
come back `[]` on every single report, and the reason is not "nothing survived":

```c
int pmu_history_hex(...) { if (s_magic != RING_MAGIC) return 0; ... }   /* rejects */
void pmu_history_clear(void) { s_magic = RING_MAGIC; ... }              /* the only writer */
```

and `main.c` calls the writer only when a report already carried samples:

```c
if (ota_report(cfg, body) == ESP_OK && n > 0) pmu_history_clear();
```

**The magic was only ever set by the function that only ran once the magic was already set.**
`pmu_sample()` wrote the ring faithfully for hours and every reader threw the lot away. The
loop was closed by §10.4ai's own fix: removing the clear from `pmu_report_history()` cured a
real bug (the history was being erased before telemetry could send it) and silently removed
the one unconditional call that armed the ring. It survived on inertia — the magic was already
set from before — until the panel was power-cycled at 12:00 on 2026-09-21, which randomised
RTC memory and made the ring permanently unreadable.

**That is the fourth instrument in this investigation to fail by answering a different question
than the one asked**, after the QSPI read path returning plausible zeros (§10.4w), the console
that resets what it measures (§10.4w), and `crash_phase` read as a crash site rather than a
task position (§10.4al). Every one of them failed the same way: *silently, in the direction of
"nothing to see"*. A reading of `[]` is indistinguishable from an honest cold boot, which is
precisely the ambiguity §10.4x invented the magic word to remove.

**0.2.28.** `pmu_report_history()` now copies the surviving ring into plain RAM first and then
arms and restarts it — unconditionally, on every boot, cold or not. Arming is that function's
job and nothing else's, the copy makes arming safe, and telemetry reads the copy, so the render
task can sample immediately without racing the reader. `pmu_history_clear()` drops the copy
rather than the ring, so a failed POST costs no evidence.

**It also samples the TCA9554 at 0x20** — input, output and configuration — alongside the six
AXP2101 registers. The scan at §10.4x named exactly two chips that could hold state across a
soft reset and be cleared by pulling the plug. One has been read for five releases and looks
innocent. The other has never been spoken to, and the BSP brings the panel's reset and enable
lines out on it. A sample is now nine bytes: `st0 st1 id dcdc_en ldo_en0 ldo_en1 | in out cfg`.

**What the next dark screen will say.** Hold five seconds; the report carries two minutes of
both chips at ten-second intervals. If an AXP2101 enable bit or a TCA9554 output drops as the
screen goes, that is the answer. If all eighteen bytes are identical lit and dark, then nothing
on this bus is doing it, and the search moves to the panel controller's own state or the OLED
supply beyond these two parts — which is worth knowing too, and is the first time that would be
a measurement rather than an inference.

#### 10.4an W4 and W4b land, as a transcription (2026-09-21)

The owner asked for the animations the PWA already plays on a poke. That was not a design task,
because `frontend/src/pet/` was written to be ported: `face.ts` says outright that "the ESP32-S3
panel will run the same model in C, so this file is the reference implementation", and `rig.ts`
that it is "in the same figure-space the panel will use, so the firmware port is a transcription
rather than a redesign". Three files crossed over, keeping their numbers:

| web | panel | what it carries |
| --- | --- | --- |
| `face.ts` | `emotion.c` | the six emotions plus `bewildered`, as lid geometry |
| `rig.ts` | `rig.c` | seventeen actions, limb poses, the figure transform, the gag skeleton |
| `variants.ts` | `variants.c` | weighted pools, per-variant cooldowns, the repetition penalty |

A poke now picks from a pool rather than doing one thing: wiggle and giggle at weight 3, boing
at 2, blush at 1, sneeze at 1, and hiccup at 0.4 with a 45-second cooldown — rare on purpose,
because a child who sees something once in three weeks talks about it for a month. Hammering it
softens the magnitude toward the 0.35 floor and never to zero, since a motionless response is
indistinguishable from a broken one.

**What did not cross over, and why.** The web rig rotates the whole figure. Rotating a 368x448
framebuffer 25 times a second is a per-pixel resample this panel should not spend, and rotating
in source space tears holes in filled shapes — so `ang` becomes a **head tilt**, the head and
eyes offset against the torso, which is the cue curious and silly actually need. `spin` is left
out of the pools rather than faked badly. Everything else — squash, offset, the breathing that
runs under even the idle pose — is coordinate arithmetic the renderer was already doing.

**The eye needed real work.** The web version clips the pupil to the eye and fills a quadratic
cheek-arc; both have closed forms. The upper lid is a half-plane in a frame rotated about the
eye's top centre, so a point test is two multiplies. The lower lid's Bezier has
`x(t) = bw(2t - 1)`, which is **linear in t** — so `t` comes straight from `x` and the curve is
`y = ly - 2t(1-t)·bend` with no root-finding. The lids cost one pass over two 60x70 boxes.

#### 10.4ao The firmware gets its first tests, and they immediately paid (2026-09-21)

`face.c` and `font.c` have been "pure C, no ESP dependencies, host-renderable" since they were
written, and **nothing ever compiled them on a host.** `firmware/host/` now does, in CI, before
the toolchain pull, in two seconds. It found three defects in code that had already built clean
for the ESP32:

1. **Missing includes.** `rig.c` used `uint32_t` and `variants.c` used `NULL` without including
   the headers that define them. ESP-IDF supplied both transitively; a different include order
   would have broken the build with no change to this code.
2. **A transcription bug the reference could not have.** `variants.ts` gets "never played" free
   from `lastPlayed[key] ?? -Infinity`. Zero-initialised C does not: a variant that had never
   been chosen looked like one chosen at boot, so early on the WHOLE pool read as still cooling,
   the picker fell through to its "everything is cooling" branch — which is allowed to
   repeat — and the first pokes of the day would repeat themselves. That is precisely the
   boredom the file exists to prevent, and it would have been invisible on the bench and
   obvious to a four-year-old in week two.
3. **The wrong language.** The harness was first written `-std=c11`, under which `M_PI` does not
   exist and every trig call fails. ESP-IDF builds this code as `-std=gnu17`, so a stricter host
   dialect was testing a language the device never compiles. It is `-std=gnu11` now.

**And one wrong test, twice, which is its own lesson.** The blink case first asserted that a shut
eye lights fewer pixels. It lights MORE: lids are drawn black inside the eye — as the web
renderer's `#000` fills are — and black is the field colour, so a happy face's cheek-raise
punches "unlit" pixels into the head and a shut eye covers them. Counting lit pixels says a blink
makes the robot bigger. The bounding box says what was actually meant: the figure's extent must
not move. Four instruments in this investigation have now failed by measuring something adjacent
to the question (§10.4am), and this is the first one that failed loudly.

#### 10.4ap The reboot gesture gets a prefix (2026-09-21)

The owner asked for three short taps, each within half a second of the last, followed by a hold
— "this will help prevent the twins from accidentally restarting it".

The hold alone was the whole gesture, and its guard was its LENGTH: §10.4p set five seconds
because 4-5 year olds were measured producing ordinary taps lasting up to 4.2 s, so five was
the first threshold outside a child's accidental press. That margin is 0.8 s, against two
children who will own these panels and have all afternoon.

**So the guard becomes a rhythm rather than a duration.** Three short taps in time, then the
hold. Mashing produces taps and it produces leans; it does not produce that sequence. Measured
against 20 000 simulated presses including leans of 3-9 s — the case a stream of short presses
could never reach, and the one the old gesture was defenceless against:

| | fires |
| --- | --- |
| hold alone (0.2.29 and earlier) | 1937 |
| three taps then hold (0.2.30) | 12 |

**Twelve and not zero, on purpose.** Three short taps in rhythm followed by a long press is a
reachable pattern, and a gesture that could never occur by accident could not be performed on
purpose either. The test asserts the ratio rather than a magic threshold, so it keeps meaning
something if the constants move.

`gesture.c` is pure and host-tested, because **both** failure directions cost something and
they pull in opposite directions: a false positive reboots a toy in a child's hands, and a
false negative strands an owner who has no terminal (CLAUDE.md #10) with no way to force a
firmware re-check. The tests state the properties the file has to have — a hold alone never
fires, slow taps never arm it, long presses do not count as taps, letting go mid-hold abandons
the whole sequence rather than leaving the panel one press from rebooting, and a completed hold
fires exactly once while the finger is still down.

**One pip per counted tap** now appears along the top edge. Without it the three taps are
invisible until the hold succeeds, and a gesture with no feedback until it works is one an
owner cannot tell from a broken panel — which is the exact failure mode this whole section of
the plan has spent six releases on.

The hold stays at five seconds. It no longer has to carry the anti-accident argument by itself,
so it could be shortened; that is a separate decision and the owner's.

#### 10.4aq Where you poke him is half the point (2026-09-21)

The owner asked for different reactions from the head, the body, the sides and the feet. That
is what the body was FOR — `rig.h` says the emotions never needed one and the gags did — and a
belly poke that does the same thing as a tap on the foot wastes it.

`face_zone()` classifies a panel coordinate against the rest silhouette, and each zone gets its
own pool:

| zone | what it answers with |
| --- | --- |
| head (and the antenna) | blush, giggle, nod, wiggle, and rarely sleep |
| body | wiggle, giggle, boing — and **fart** and **burp**, because a poke in the stomach producing a fart is the joke a four-year-old is actually asking for |
| arms / sides | wiggle, wave, shimmy, giggle, and rarely peekaboo |
| legs / feet | jump, boing, dance, bop, and rarely hiccup |
| background | the original poke pool, because a tap that misses must still answer |

Two deliberate details. **The zones follow the flip**: a tap on his head is his head whichever
way up the panel is held, which matters precisely because a child holds it any which way.
**Transient action offsets are not applied** — a hitbox that leaps during a jump is one nobody
can learn, so the rest silhouette is always the target.

**THE TOUCH CONTROLLER'S ORIENTATION IS UNMEASURED, AND THIS TIME THAT IS SAID OUT LOUD.**
Nothing in this firmware has ever read a coordinate from the CST820; it reported a finger
count and nothing else. Whether its axes match the display's is exactly the question §10.4ae
and §10.4af burned three releases on for the accelerometer, by reasoning about it instead of
measuring. So 0.2.31 does not reason:

- a marker ring is drawn where the firmware believes the finger was, riding the flinch so it
  fades with the recoil. **If the dot is not under the finger, the mapping is wrong.**
- `tap: [x, y, zone]` goes out in telemetry, so the box can say how wrong.

One tap answers it. That is the whole difference from the accelerometer episode, and it cost
about fifteen lines.

#### 10.4ar The speech models fit, and they ship before the code (2026-09-21)

The owner asked whether the board does speech recognition on its own. It does, and **the
partition for it was reserved at the start** — `model, data, spiffs, 0xAA0000, 0x380000`,
3.5 MB, commented "reserved for ESP-SR wake-word models (W6); nothing uses it yet". That
decision, taken when reserving was free, is the reason this is a build rather than a reflash.

**Measured rather than estimated**, by adding esp-sr 2.5.4 and reading what its packer emits:

```
ESP-SR Models Report
  - fst          (9.42 KB)
  - mn7_en       (2686.65 KB)
  - wn9_hiesp    (284.16 KB)
  Recommended Partition Size: 2982K
```

`srmodels.bin` is **2.91 MB against 3.50 MB reserved — 17% spare**, and the build resolved the
flash offset to `0xaa0000` on its own. MultiNet**6** English is 3.7 MB and would not fit, so
MultiNet7 is not merely the better choice, it is the only English one that works.

**What it is, precisely.** Command-word recognition, not dictation: up to 200 phrases, English,
recognition inside 500 ms, entirely offline. It cannot transcribe a sentence. It can be told
"jump", and say so on the glass.

**The obstacle was never flash, it was the delivery path.** `esp_https_ota` writes app slots
and nothing else, so a data partition is unreachable over the air — and both panels are in the
twins' rooms. The first answer drafted here was a model-fetch-over-HTTP in the firmware, about
eighty lines plus a box route, to preserve the no-cable rule.

**That was over-engineering, and one fact killed it: the command vocabulary is not in the model
file.** MultiNet phrases are supplied at runtime as phoneme strings (`esp_mn_commands_update()`),
so adding a phrase, dropping one, or retuning a word a four-year-old cannot say is an ordinary
OTA. `srmodels.bin` changes only if the wake word or the model generation does — close to
never. One USB write per unit is therefore enough, and "USB" here means carrying the panel to
the box and pressing a button in the PWA, not a terminal. Rule #10 survives.

So the models ship FIRST, before any code that uses them:

- `sdkconfig.defaults` selects `wn9_hiesp` and `mn7_en`. The app does not call esp-sr, the
  linker drops it, and the app image is **byte-identical** — verified against the committed
  hash. What the dependency produces is `srmodels.bin` and nothing else.
- `firmware/dist/` carries it, `scripts/firmware-dist.sh` copies it, and the `firmware` CI job
  checks it — **by content, not by bytes**, for the reason below. It is the one image in that
  set not built *from* this source, which is exactly why it needs a check: nothing else would
  notice it going stale.
- `ARTIFACT_IMAGES` gains `srmodels.bin -> 0xaa0000`, so it rides the flash path that already
  exists, last in offset order.

**Cost recorded honestly.** The esp-sr component is 308 MB and the clean firmware build goes
from about 50 s to 1 m 55 s locally; CI will also pay the download. That is the price of the
models being verifiable rather than a committed blob nobody checks.

#### 10.4as `srmodels.bin` is not reproducible, and the check had to change (2026-09-21)

The first CI run on the models failed: `MISMATCH: firmware/dist/srmodels.bin`. The blob CI
built was **exactly the same size** as the committed one — 3,052,231 bytes — and differed in
2.9 million of them.

Not a stale commit, and not a different esp-sr: the lock pins 2.5.4 on both sides. Downloading
CI's own artifact and diffing the headers showed it immediately — CI wrote `mn7_en` first, this
machine wrote `fst` first. `model/pack_model.py` collects models with `os.walk` and never
sorts, so **the order is whatever the filesystem hands back**, and one reordered model shifts
every offset after it.

`CONFIG_APP_REPRODUCIBLE_BUILD=y` is what makes the byte-for-byte check possible for the other
three images, and it cannot help here: this is not our build. Three builds on this machine give
the same hash; a build on a different machine does not.

**So the check compares contents instead of layout.** `scripts/srmodels-inventory.py` parses
the header — the format is four bytes of model count, then a 32-byte name and file count per
model, then a 32-byte name plus offset and length per file — and prints every file as
`model/file sha256 length`, sorted. CI diffs that inventory against the committed blob's.

That is byte-for-byte equality modulo an ordering nobody chose, and it still catches everything
the original check was for: a wrong wake word, a missing model, a truncated file, a stale
commit. Verified both ways before pushing — the real CI blob passes, and flipping a single byte
inside `mn7_data` fails with the offending file named.

`SHA256SUMS` keeps its `srmodels.bin` line, because the api verifies images against it at flash
time and that guarantee (this committed file is not truncated or corrupt) is real and separate.
CI just checks that line against the committed blob rather than against its own build.

**The general lesson, and it is the session's fourth:** "the same source must produce the same
bytes" was a property of images we build with a flag that guarantees it. Extending the rule to
a vendor artifact assumed the guarantee came with it. The failure was loud and cost one CI
round, which is the cheap way to find out — but the assumption was the same shape as every
other one this investigation has had to unwind.

#### 10.4au The version is IN the image, and twice it was not (2026-09-21)

`firmware` CI failed on 0.2.35 and again on 0.2.36 with `MISMATCH:
firmware/dist/jbrain-endpoint.bin`. Not the `srmodels.bin` ordering problem of §10.4as — this
is the app image, built with `CONFIG_APP_REPRODUCIBLE_BUILD=y`, which is exactly the check
that is supposed to be exact.

A clean-room rebuild (`rm -rf build sdkconfig`, since `sdkconfig` is generated and gitignored)
gave a binary of **identical length** and a different hash. 66 bytes differed. Reading the
app descriptor at 0x20 said it in one line:

```
dist   version: 0.2.35   elf_sha256: 3183764972...
fresh  version: 0.2.36   elf_sha256: 7617657390...
```

**ESP-IDF compiles `firmware/version.txt` into the image**, so bumping it *after* `idf.py
build` stamps the previous number into the bytes that ship. The 66 bytes are the version
string and the ELF hash that covers it. Both failures were the same slip in the same order,
and on the way to finding it I had written in this session that the file was "metadata only,
not compiled in" — wrong, and wrong in the direction that makes the mistake invisible.

So it is a check rather than a thing to remember: `scripts/firmware-dist.sh` now reads the
built image's descriptor and **refuses to copy an image whose embedded version does not match
`version.txt`**, with the fix in the message. Confirmed to fail on a mismatch and pass on a
match before being kept.

Worth noting what CI got right here. The byte-exact comparison was doing its job perfectly —
it caught a real defect (a panel would have reported the wrong version to the box, which is
the one number the owner uses to tell whether an update landed) and it named the file. What it
could not do is say *why*, and two rounds went into a difference that the app descriptor
answers immediately. A failing image comparison should be read at 0x20 first.

#### 10.4av The panel listens, and the repartition that cost (2026-09-21)

> *"where we at now with the voice to text recognition I would still prefer to have it so that
> it'll just try and listen to the microphone and put text scrolling on the bottom as it
> recognizes it"*

Built, with the limit stated rather than papered over. **MultiNet resolves a list; it does not
transcribe.** `firmware/main/vocab.c` holds 22 phrases and the model answers with *which one*
it heard, offline, in under half a second. The open-vocabulary alternative is Whisper tiny int8
at ~75 MB against 8 MB of PSRAM (§10.4ar) — two orders of magnitude, not a tuning problem. So
the ticker shows the phrase the model resolved and shows **nothing** when it resolved nothing:
a four-year-old can read a miss and cannot read an invention, and a hallucinated command the
robot then acts on reads as the toy being broken.

**No wake word**, because "just try and listen" was the request. That single decision shapes
everything else. Every phrase is always live, so each is two words minimum — a one-word
always-on vocabulary fires at the television — lowercase a-z only (the grapheme-to-phoneme
pass silently refuses anything else, leaving the panel deaf to exactly one thing with nothing
on screen to say so), and no phrase a prefix of another. The host suite enforces all three,
plus that every letter in the vocabulary has a glyph and that no two glyphs draw the same
shape: the alphabet was hand-entered this session, and a copy-pasted bitmap is invisible until
someone reads a word on the glass.

The one-owner rule extends unchanged: `audio.c` already owns the codec, so it is the only
caller of `speech_feed`, which accumulates to the front end's chunk size (not the capture's
40 ms) and hands off. MultiNet runs pinned to **core 1**; core 0 carries Wi-Fi. There is no
AEC, and not by choice — one ES8311 and no ES7210 means no reference channel (§10.5 A), so the
front end is told `"M"` rather than handed a fake channel to cancel against silence.

The red dot beside the ticker is the **ICO Children's Code recording indicator**, asserted by
three tests against the real state: present whenever the microphone is open, brighter while
someone is talking, absent when it is not. "Muted is a promise" now has something keeping it.

##### And then it did not fit

Linking esp-sr took the app from 1.16 MB to **3.05 MB**. `factory` was 1.5 MB — and `factory`
is exactly where a USB flash writes. `idf.py build` reports this as a **warning** and exits
zero, so a build that could not be flashed onto a panel at all still looked successful.

The app slots are now 3.5 MB each, equal by construction. The 3 MB came from the OTA slots
(4.5 → 3.5), which means **`model` and `storage` keep their exact offsets** — worth arranging
deliberately, because every constant that moves is another place a panel can be bricked from
and the box's flasher hardcodes `MODEL_OFFSET`. Headroom is 12.9% per slot.

**A resize makes `otadata` mandatory on every USB flash, and it was not being written.** A
panel that has ever been OTA'd has `otadata` naming an OTA slot; a USB flash writes `factory`,
so the panel would ignore the image just written. That was survivable while the layout was
fixed and is not survivable across a resize, because the stale pointer now names a slot at a
*new* offset holding the middle of an old image — the cable this design exists to avoid.
`ota_data_initial.bin` now ships in `dist/` and the flasher writes it at `0xf000`.

`test_two_full_size_ota_slots_survive` asserted `>= 4 MiB` and had to change, which is the
right moment to notice that a size constant was standing in for something else. What has to be
true is that **every app slot is bigger than the image that actually ships**, so that is now
asserted against the committed bytes, alongside no-gaps-or-overlaps and all three slots equal.
Each was confirmed to fail against a deliberately broken table before being kept.

##### A path filter hid a stale test for six versions

`supervisor/tests/test_deploy_scripts.py` asserts that `firmware/dist/SHA256SUMS` names exactly
the images the box flashes — and it still named three when `srmodels.bin` had been there since
0.2.31. It never failed, because the `supervisor` job is gated on a path filter that lists
`supervisor/**` and `deploy/sdr/**` and says nothing about `firmware/`. A firmware-only PR
skipped the whole job. The filter now includes `firmware/dist/**` and `firmware/partitions.csv`.

The general shape, and it is worth carrying: **a path filter encodes where the code is, and a
test's subject is not always where the test lives.** The same file also asserts the recovery
offsets in `partitions.csv` against the api's constants — the exact thing this section changed,
in a job that a firmware-only PR would not have run.

#### 10.4aw 0.2.37 listened perfectly and then could not start a radio (2026-09-21)

The owner flashed 0.2.37 and the panel went into a **boot loop, every 3.2 seconds**. The flash
itself was flawless — new partition table, `otadata` reset, booting from `factory`, models
found — and so was the feature:

```
I (1573) MODEL_LOADER: Successfully load srmodels
I (1593) AFE: AFE Pipeline: [input] -> |VAD(WebRTC)| -> [output]
I (2833) speech: vocabulary: 22 accepted, 0 refused
I (2833) speech: listening: mn7_en, 512 samples per feed
W (3013) wifi:malloc buffer fail
E (3023) wifi:Expected to init 10 rx buffer, actual is 4
ESP_ERROR_CHECK failed: esp_err_t 0x101 (ESP_ERR_NO_MEM) at ./main/net.c line 60
abort() was called
```

**This board has 8 MB of PSRAM and 140 KB of internal RAM**, and the boot log says so plainly
(`87 KiB` + `21 KiB` + `32 KiB`). The Wi-Fi driver's DMA descriptors can live nowhere but
internal. ESP-SR's front end **defaults to allocating internal**, started 1.5 s into boot, and
took it — so `esp_wifi_init` got ESP_ERR_NO_MEM at 3.0 s.

Three separate mistakes, and only the first is about memory.

**1. The front end was never told to use PSRAM.** `afe_config_init` defaults to internal, and
nothing in this firmware overrode it. It is a compute pipeline reading a ring buffer; PSRAM at
80 MHz feeds it fine. One line: `memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_PSRAM`.

**2. The ordering was a race nobody chose.** `speech_start()` ran inside the render task at
~1.5 s while `net_connect` ran from `app_main` at ~3.0 s, so the recogniser always won. It now
starts from `main.c` **after** the radio, after the manifest fetch over TLS, and after any
pending update has rebooted the panel. Listening is the last thing the panel earns, because
being updatable is the first — and the repo already said so in those words.

**3. `ESP_ERROR_CHECK(esp_wifi_init(...))` turned a shortage into a brick.** This is the one
that actually cost a cable. §10.4 and 0.2.32 established that *a panel with no Wi-Fi is still a
robot* — but only for a failed JOIN. A failed INIT is the same fact arriving one call earlier,
and it aborted. It now returns, logs the free internal heap, and the panel carries on drawing
and answering taps while the OTA loop retries.

Plus the guard that makes the class of bug impossible rather than fixed: **`speech_start()`
refuses to start below 48 KB of free internal heap**, saying why. The recogniser is never
allowed to cost the panel its radio, because a panel that cannot be reached is the one state
this design calls unrecoverable.

**What this cost and what it did not.** It cost one USB flash. It did not cost the recovery
ladder: the panel was reachable over USB throughout, and `panel-console` read the fault in one
call without the owner describing anything. Worth noting against §10.4av's own reasoning —
which correctly said an OTA would have been the *safer* first exposure, because rollback
catches exactly this and a flash to `factory` does not. That was the right analysis and the
flash happened anyway; the lesson is to say "then don't flash yet" rather than only explain why
flashing is riskier.

**And a miscount, corrected.** `vocab.c` has **22** phrases; the plan, the README and the PR
all said 23. The panel's own boot line is what caught it — `22 accepted, 0 refused` — which is
the argument for logging a count rather than restating one.

#### 10.4ax 117 KB of scratch for a model that never runs (2026-09-21)

0.2.38 stopped the boot loop — every guard fired, the panel drew, it said why:

```
E (1854) net: wifi init failed (ESP_ERR_NO_MEM) — free internal heap 18095 B.
W (1874) speech: only 18095 B of internal heap free — not starting the recogniser
```

**But 18 KB was free BEFORE the recogniser tried anything**, so §10.4aw's diagnosis was
incomplete. Runtime allocation was never the whole story. `idf.py size-components`:

| | |
|---|---|
| `libnsnet3.a` | **117,272 bytes of `.bss`** |
| static DIRAM | 308,895 / 341,760 — **90.38%** |

That is the scratch buffer of **nsnet3, a deep noise-suppression model this firmware does not
select and never runs**. `CONFIG_SR_NSN_WEBRTC=y`, and the pipeline the front end actually
builds is `[input] -> |VAD(WebRTC)| -> [output]` — no NS in it. esp-sr links the archive
unconditionally (its `CMakeLists.txt` adds it whenever it exists for the target, because
`libnsnet.a` references `esp_nsnet3`), so the panel was paying 38% of all its static RAM for a
model that is switched off.

It cannot be dropped by patching the component — the next dependency fetch would undo it — but
it does not need to be. It is **`.bss`**: zero-initialised scratch, no image cost, no start-up
copy, and it does not care where it lives. `main/linker.lf` maps that one archive to
`extram_bss` and `CONFIG_SPIRAM_ALLOW_BSS_SEG_EXTERNAL_MEMORY=y` allows it;
`CONFIG_SPIRAM_TRY_ALLOCATE_WIFI_LWIP=y` lets the radio take its non-DMA buffers from PSRAM too.

| | before | after |
|---|---|---|
| static DIRAM | 308,895 (90.38%) | **177,947 (52.07%)** |
| DIRAM remaining | 32,865 | **163,813** |

**The lesson is about instruments, not memory.** Two versions in a row were misdiagnosed from
the boot log, because the boot log did not carry the number that decides it. Finding the real
cause took `size-components` on a host toolchain — which is precisely what nobody has when a
panel is on a bedroom wall, and exactly the dependency CLAUDE.md #10 exists to remove.

So the panel says it itself now. `mem.c` prints free **and largest-block** for internal RAM and
PSRAM (they fail differently: a driver wanting one contiguous 32 KB buffer fails with 60 KB
free and fragmented, and "free" alone makes that look impossible), at every boot stage that
takes a big bite — `boot`, `display`, `imu`, `wifi`, `pre-speech`, `post-speech`. And the blit
error that printed 150 identical lines during the 0.2.38 capture now logs once, then every
hundredth, with the heap attached, and says when it recovered.

#### 10.4ay "Listening" and hearing are different claims (2026-09-21)

0.2.39 came up on the network, reached the box, and logged
`speech: listening: mn7_en, 512 samples per feed`. The owner talked to it and **nothing was
ever recognised.** Every line said the feature was working.

The suspect is the one Espressif's own examples guard with an assert rather than handle:
`multinet->detect()` reads **`get_samp_chunksize()`** samples from the pointer it is given and
trusts the caller, while the front end returns **`get_fetch_chunksize()`** samples. They are not
the same number. Feeding one straight to the other misreads the length at every frame boundary,
so the model sees an audio stream that skips or repeats a few milliseconds 30 times a second —
which decodes to nothing, silently, forever, while the log still says "listening".

`feed_multinet()` now accumulates the fetched audio and hands the model exactly the chunk it
asks for, so the two sizes never have to agree. Both are logged at start-up, with `(equal)` or
`(DIFFERENT — buffered)` said out loud, because a number nobody printed is how this survived.

**The deeper failure is a claim that could not be checked.** "Listening" only ever meant "the
models loaded and a task is running" — it never meant audio was arriving or being decoded, and
nothing on the panel distinguished a dead microphone from a deaf model. So the detect loop now
reports, every three seconds: frames fed, frames fetched, the **loudest sample** in the frame,
the VAD verdict, and the front end's own dBFS. And a MultiNet timeout prints **`raw_string`** —
what the decoder actually heard before the command graph rejected it. With those, "it did not
hear me" splits into four distinguishable faults: no audio, quiet audio, audio that does not
trigger VAD, and a decode that misses the vocabulary.

#### 10.4az The chunk theory was wrong, and the logging said so in one boot (2026-09-21)

0.2.40 shipped a buffer between the front end and MultiNet on the theory that
`get_fetch_chunksize()` and `get_samp_chunksize()` differ. The panel's first boot answered it:

```
I (21707) speech: chunks: feed 512, fetch 512, multinet 512 (equal)
```

**They were never mismatched.** The buffer is harmless and stays — it removes an assumption
that Espressif's own examples only `assert` — but it was not the bug, and one line of logging
retired the hypothesis in the time it took to read it.

What the same boot *did* establish, which no amount of reasoning had:

```
peak  3407 | vad SPEECH  | -19.7 dBFS     <- the microphone works, VAD agrees
speech: timeout, raw decode: ''           <- the model decodes NOTHING
```

Audio arrives, the front end calls it speech, and MultiNet returns an **empty** raw decode —
not a wrong word, nothing at all. That is the signature of audio below the level the model can
work with, and the cause is a default nobody looked at:

**`afe_config_init` defaults `agc_mode` to `AFE_AGC_MODE_WAKENET`, whose own header says the
gain is "calculated by wakenet model IF WAKENET IS ACTIVATED".** This firmware disables
wakenet — that was the owner's request, "just try and listen" — so the default applies **no
gain at all**. Every other part of the pipeline was correct and the audio simply arrived too
quiet to decode.

Fixed by naming what had been left to a default: `agc_init = true`,
`agc_mode = AFE_AGC_MODE_WEBRTC` (the mode that needs no wake word), a -3 dBFS peak target,
and the whole front-end configuration logged at start-up so the next reader sees it rather
than inheriting it. The ES8311's own PGA goes 30 → 36 dB alongside it; 42 is the part's
maximum and pinning it there raises the noise floor with the signal, so the remaining headroom
is left to the AGC and to a measurement.

**Three versions, three wrong first guesses, and the pattern is the same each time:** every one
was a value this firmware never set and never printed. The memory mode (§10.4ax), the chunk
sizes (above), and now the gain mode. A default you did not choose is not a decision, and a
default you do not log is not visible. The front end now prints its own configuration.

#### 10.4ba The AGC fix blacked out the panel (2026-09-21)

0.2.41 turned the front end's AGC on to cure an empty decode. **The owner's screen went
black.** The panel's own logging named the cause in two lines:

```
mem: pre-speech     internal 154435 free / 81920 largest
mem: post-speech    internal  51631 free /  9728 largest
E display: blit: ESP_ERR_NO_MEM (failure 201)
```

Switching AGC on cost ~103 KB of internal RAM and — the part that actually mattered —
**collapsed the largest contiguous block from 81,920 bytes to 9,728**, which is smaller than
the buffer the SPI driver needs to push a frame. Every blit after the recogniser started
failed, so nothing reached the glass. This is exactly the free-versus-largest distinction
§10.4ax added the logging for, and it is the second time that distinction has been the answer.

`memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_PSRAM` was already set and did not prevent it. A
preference the component may decline is not a guarantee, so that field is now logged too.

**Reverted, and the gain taken from the codec's own PGA instead** (36 → 42 dB, the part's
maximum), which costs no RAM. The noise floor rises with the signal there, which is a real
cost and the reason 36 was tried first — but a panel that draws is worth more than a clean
noise floor, and the peak is logged so the next move is a reading.

##### And the reason it could black out at all, fixed in the same version

The AGC was the trigger; the fragility was ours. `spi_bus_initialize` sizes its DMA descriptor
chain ONCE from `max_transfer_sz`, and that was set to `sizeof(stripe)` — **11,776 bytes**, the
colour-bar buffer — while the render loop pushes a full **329,728 byte** frame, 28 times
larger. Every transfer past the reservation makes the SPI driver allocate descriptors **at
transfer time, out of internal RAM, twenty-five times a second, forever.**

So the display was competing for memory with everything that starts after it, every frame, and
had no defence. It is now reserved for a whole frame at bus init — before Wi-Fi, before TLS,
before ESP-SR, when 257 KB of internal RAM is free and the largest block is 163 KB.

This also retires a symptom that was **dismissed** in §10.4ax: the "one dropped frame per box
check-in", waved through as acceptable because it coincided with the TLS handshake. Same bug,
smaller amplitude. One instance of it was rationalised and the other was a black screen — which
is the argument for chasing the small version of a fault while it is still small.

**What this leaves open:** it was never confirmed that the empty decode was a level problem at
all. The captures that motivated the AGC change were taken with **nobody talking to the
panel** — an instrument built and then read against silence. The next reading is taken while
someone is speaking, and no further gain change is made before that.

#### 10.4bb The DMA reservation worked, and the gain was never checked (2026-09-21)

0.2.42 on the panel, measured over 70 s:

| | 0.2.41 | 0.2.42 |
|---|---|---|
| blit failures | 200+ | **3** |
| largest free block after the recogniser starts | 9,728 | 16,384 |

**The reservation works**, and the owner's photo of 0.2.41 had already confirmed the mechanism
better than the logs did: the panel was drawing the top of the frame and leaving the bottom
stale, with green garbage between. That is a **chunked transfer failing partway** — at 11,776
bytes of `max_transfer_sz` a 329,728-byte frame is cut into ~28 pieces, the early ones
allocate their descriptors and the later ones do not. Fully black was the same fault with the
first chunk failing too.

**It is not free.** Reserving a whole frame costs ~138 KB of internal RAM at bus init and takes
the largest block from 163,840 to 31,744 before anything else starts. That is a real price and
it is why the recogniser now begins with 87 KB rather than 154 KB. Recorded because the next
person to add a feature needs to know the budget shrank, not discover it.

##### And the gain was a claim, not a reading

```c
esp_codec_dev_set_in_gain(s_codec, MIC_GAIN_DB);        /* return value discarded */
ESP_LOGI(TAG, "es8311 ready: ... in %.0f dB", MIC_GAIN_DB);  /* prints what was ASKED FOR */
```

Raising the gain 30 → 42 dB made the measured level go **down** by about the same 12 dB:

| build | asked | measured peak |
|---|---|---|
| 0.2.40 | 30 dB | 1,261–3,407 |
| 0.2.41 | 36 dB | 322–3,493 |
| 0.2.42 | **42 dB** | **194–757** |

That is what an out-of-range value that wraps looks like, and an unchecked setter is what let
it be believed. The call is now checked, falls back to 30 dB when refused, and the log prints
`accepted` or `REFUSED` rather than restating the constant.

**Four for four, and the pattern is now the whole lesson of this feature:** the memory mode
(§10.4ax), the chunk sizes (§10.4az), the AGC mode (§10.4az) and the mic gain were each a
value this firmware set and never read back. Every wrong diagnosis in this sequence traces to
exactly that. "Set it and log the constant" is indistinguishable from "set it and have it
refused" — and on a device with no terminal, indistinguishable means invisible.

**Still not measured:** every audio capture so far was taken with nobody talking to the panel.
No further gain or front-end change is made before a reading with a voice in the room.

#### 10.4bc The frame was copied into internal RAM every blit (0.2.44, 2026-09-21)

**The black screen, root-caused — and §10.4bb's `max_transfer_sz` change was treating a
symptom and made it worse.**

`esp_lcd_panel_io_spi` only hands the SPI driver a PSRAM pointer when `psram_dma_direct` is
set on its IO config, and `CO5300_PANEL_IO_QSPI_CONFIG` never sets it. Without it
`setup_dma_priv_buffer()` in `spi_master.c` takes the `!use_psram` branch: it
`heap_caps_aligned_alloc`s an **internal copy of every chunk** and memcpys the frame into it,
per transfer, forever. The framebuffer lives in PSRAM; every byte of it was being moved
through internal RAM on its way to a bus that could have read it where it lay.

| `max_transfer_sz` | what the driver then does | what the owner sees |
|---|---|---|
| 11,776 (0.2.41 and earlier) | 28 small internal allocs per frame | top of the frame new, bottom stale — **the owner's photo** |
| 329,728 (0.2.42–0.2.43) | ONE alloc of 329,728 B from a 341 KB pool | black, whenever anything else is running |

The owner's 0.2.43 photo settled it: the pet drawn across the top third and **the boot colour
bars still showing underneath**. Those bars are written once at startup (`display.c`), so the
lower two-thirds of that panel had not been written since boot.

**0.2.42's number was a measurement artefact and it was reported here as an improvement.**
"Blit failures fell from 200+ to 3 over 70 s" was counted without its denominator: the idle
loop redraws rarely, so there were only a handful of attempts in that window and nearly all of
them failed. A rate needs both terms. §10.4bb is left standing with this correction attached
rather than edited, because the wrong conclusion is the useful part.

`io_cfg.flags.psram_dma_direct = true` removes the bounce buffer entirely, and
`max_transfer_sz` goes back to the stripe size — there is no longer a large internal
allocation to size.

#### 10.4bd The bird had no channels of its own (0.2.45, 2026-09-21)

The owner, on the ostrich: *"They lack motion and funness that the robot has... it doesn't have
the arms. Maybe like kicking legs and moving the tail and the whole head and neck."*

That is exactly the shape of the bug. Every expressive channel on this form was **derived from
the arm angles**, because the robot was built first and the ostrich was fitted to its rig. A
bird has no arms, so the one place the arm signal was spent — the tail flap — carried the whole
performance.

And §10.4at's fix for that made it worse. Driving the flap off `arm_l` alone had made `wave`
render zero pixels, so it was changed to the **mean** of the two arms:

```c
const float dev = ((st->rig.arm_l - 12.0f) + (-st->rig.arm_r - 12.0f)) * 0.5f;
```

Dance, bop, shimmy, wiggle and giggle all swing both arms the **same** way, so that mean is a
constant and the tail stopped moving for every one of them. One action rescued, five broken,
and the suite was silent because `rig_pose_t` still changed. The signed larger deviation tracks
whichever arm is actually doing something.

**The real fix is that the bird gets channels of its own** — `neck`, `bob`, `tail`, `step` and
`crest` on `rig_pose_t`, filled by `bird_channels()` and ignored by the robot. Measured as the
largest change between *consecutive* frames, which is what a child actually sees:

| | before | after | |
|---|---|---|---|
| `wave` | **999** | 5,936 | a bird waves its neck; it has nothing else to wave |
| `blush` | **465** | 1,712 | |
| `wiggle` | 6,915 | 13,120 | |
| `nod` | 9,601 | 13,788 | the whole neck, not a body offset |
| `sleep` | 3,260 | 5,318 | |
| `dance` / `bop` / `shimmy` | 18,030 / 18,092 / 18,081 | 20,386 / 21,523 / 21,179 | |

**Those three were byte-for-byte identical** at a matched clock — measured, not inferred. They
share one `case` in `rig_for`, which is a decision about the *robot*; the bird inherited one
animation under three names. They are separate in `bird_channels`: dance sweeps and strides,
bop pumps the head on the beat, shimmy is a fast tail over slow feet.

`crest` is not authored per action. It is the same curve evaluated 60 ms ago — light things
trail heavy ones — so every action gets follow-through for free, including ones added later.

**The robot is byte-identical across all 16 actions**, checked frame by frame, which is the
point of putting these on their own channels.

##### A test that measured the wrong thing, twice

The neck segments were drawn at a bare `ox` while the head sat at `ox + tilt`, so a tilt slid
the head sideways and dragged the join. The first two attempts to test it both passed on the
broken renderer:

1. *"no empty row between body and head"* — there never is one. A 128 px head overlaps a 34 px
   neck at any tilt this rig produces. Nothing is lost; it moves.
2. *"the neck's centre tracks the head's centre"* — sampled at dy −75, which is **inside the
   head**. It was comparing the head with itself and reporting agreement.

The head reaches down to about dy −70. At dy −60, which is neck:

| | tilt 0 | tilt +14 |
|---|---|---|
| before | 170..206 (**w 37**) | 170..218 (**w 49**) — left edge pinned, right edge dragged |
| after | 170..206 (w 37) | 185..221 (**w 37**) — translates, keeps its width |

So the property is *a tilt may lean the join but may not widen it*, and each neck segment now
carries its share of the head's displacement. Recorded at length because two plausible tests
passed against a renderer with the defect still in it, which is the same failure mode as
§10.4at's `test_every_action_moves`: **a test that does not render cannot see a rendering bug,
and a test that renders the wrong pixels is no better.**

#### 10.4be Who copies the frame out of PSRAM (0.2.46, 2026-09-21)

0.2.44 drew a full frame for the first time in four versions — the owner's photo showed the
whole bird with no boot bars under it — and then the panel went black again, and the next
photo after that showed the frame **torn into vertical bands**: fragments of the ostrich,
stale content, garbage. The panel's own log named the new fault in two lines:

```
E spi_master: DMA TX underflow detected
E lcd_panel.io.spi: panel_io_spi_tx_param(222): recycle spi transactions failed
```

**`psram_dma_direct` traded a memory fault for a bandwidth fault.** Reading the framebuffer
straight out of PSRAM couples the SPI clock to PSRAM latency, and PSRAM here is contended:
ESP-SR runs continuously on the other core and holds 3 MB of it. When the DMA cannot refill
the SPI FIFO in time the transfer underflows — the bus keeps clocking and shifts out whatever
is in the FIFO, which is the torn photo — the driver is left holding transactions it never
recycles, and every call after that returns `ESP_ERR_INVALID_STATE`. The render task stops and
the screen stays black.

So the bounce buffer was not only waste. It was also **decoupling the bus from PSRAM**, and
0.2.44 removed a function along with the bug. Four versions argued about who should copy the
frame and where the copy should live, and every answer that let the *driver* decide was wrong:

| | who copies, and where to | how it fails |
|---|---|---|
| 0.2.41 | driver, allocating 11,776 B per chunk | 28 allocations a frame; one fails and the rest of the frame is never written |
| 0.2.42–43 | driver, allocating 329,728 B per frame | cannot succeed at all once anything else is running |
| 0.2.44–45 | nobody; DMA reads PSRAM directly | underflows under PSRAM contention and poisons the SPI driver for good |
| **0.2.46** | **us, into one static internal buffer** | — |

`blit_frame()` memcpys each stripe into `stripe` — already static, already `DMA_ATTR`, and
idle after the boot colour bars — and hands the driver a pointer that is *already* internal
and DMA-capable, so `setup_dma_priv_buffer()` allocates nothing and reads no PSRAM. The buffer
wants to be internal so the DMA never waits on PSRAM, and it wants to already exist so a blit
can never depend on the heap. One object satisfies both. The cost is 28 memcpys of 11,776
bytes per frame — 1.6 MB/s at the face's 5 fps — which is the cheap half of the trade.

`max_transfer_sz` goes back to `sizeof(stripe)`, and the correction there is worth keeping:
0.2.42 raised it to a whole frame to stop transfer-time allocation, but the allocation that
mattered was the bounce **buffer**, not the descriptors. Asking for a frame-sized one made it
unsatisfiable. **§10.4bc also claimed this revert shipped in 0.2.44 and it did not** — the
0.2.44 commit set `psram_dma_direct` and left the reservation alone. It ships here.

##### The nothing in the log was the symptom

0.2.44 logged one blit failure at t=2.2 s and then **nothing at all**, and a clean-looking log
is what made the black screen take an owner's photograph to find. The blit error is rate
limited to every hundredth failure, so once the render task stopped attempting blits the
limiter never fired again — no failures, no recovery line, no way to tell a frozen panel from
a quiet one.

`render: N frames ok, M failed | internal largest K` now prints every 10 s. Anything that can
stop has to say so on a timer; the absence of an error is not evidence of health. The largest
free internal block rides along because it is the number that has explained this fault twice.

##### And the panel heals itself

An underflow leaves the SPI driver holding transactions it never recycles, and from then on
every call returns `ESP_ERR_INVALID_STATE` — **permanently, until someone power-cycles the
unit**. This fix removes the cause, but "the display can enter a state only a human with hands
on the hardware can leave" is a property worth removing on its own: this panel lives in a
child's bedroom and its owner has no terminal (`CLAUDE.md` rule 10).

250 consecutive failed blits — ten seconds at 25 fps — and the panel restarts itself. A reboot
costs about four seconds of colour bars and is recoverable; a black screen is not. The 60 s
uptime guard is what keeps that trade honest: a fault present from boot would otherwise cycle
forever, and a reboot loop is no better than a freeze. Past the guard, a panel that cannot draw
for ten seconds has nothing to lose by starting over.

#### 10.4bf The screen works, the mic works, and both were half-right (0.2.47, 2026-09-21)

0.2.46's stripe blit ended the black screen: `render: 65 / 188 / 318 frames ok, **0 failed**`
across 68 s, and the owner's photograph shows a whole ostrich. The underflow is gone, the
`INVALID_STATE` is gone, and the largest free internal block sits at 31,744 with everything
running.

**And the recogniser decoded a phrase for the first time in this feature.**

```
I (16874) speech: heard 'jump up' p=0.19
```

Every earlier conclusion about the microphone was drawn against a silent room. With someone
actually speaking, the levels are not what any of those readings suggested:

| | measured with nobody talking | measured with a voice at the panel |
|---|---|---|
| peak | 194–3,493 | **32,768** |
| level | −54 dBFS | **−2.0 dBFS** |

Three in-vocabulary phrases have now been confirmed working from the owner's photographs:
**"pick a new color"** (the bird turned yellow), **"jump up"** and **"do a dance"**.

##### Both halves need correcting, and one of them is mine

**The blit tore, and 0.2.46 shipped the tear.** `esp_lcd_panel_draw_bitmap` QUEUES the
transfer and returns — which this panel had been saying in its own error message for three
versions, *"recycle spi transactions failed"*, the driver reclaiming transactions queued by
earlier calls. With the vendor macro's `trans_queue_depth` of 10, up to ten transfers can be
reading the buffer while the next `memcpy` writes it. 0.2.46 used ONE shared stripe, so the
DMA read bytes already overwritten by the following stripe: thin horizontal bands of the
figure displaced sideways, in the owner's photo, on the very version that fixed the black
screen. Two buffers and `trans_queue_depth = 1`. The driver source is what settles that this is the
right fix rather than a plausible one (`esp_lcd_panel_io_spi.c`, `tx_color`):

```c
if (spi_panel_io->num_trans_inflight < spi_panel_io->queue_size) {
    lcd_trans = &spi_panel_io->trans_pool[...];              /* queue, do not wait */
} else {
    ret = spi_device_get_trans_result(..., portMAX_DELAY);   /* BLOCK */
}
```

With `queue_size = 1` the second call blocks until the first completes, so exactly one
transfer is ever in flight and the buffer about to be filled is by definition the other one.

**Reading it also found the same bug in the meter**, which the face blit had hidden. `s_strip`
is one buffer, refilled 25 times a second and pushed on every frame, so the buffer being
written is the one the previous transfer may still be reading. Its window is much narrower —
8,832 bytes clear in well under the 40 ms between meter updates — which is precisely why it
would pass every test on the bench and corrupt the bar in a bedroom. Doubled too. The owner
reports the panel freezing at random on 0.2.46, and a transfer reading a buffer that is being
rewritten can desync the panel's command stream rather than merely its pixels, so these two
are the same fault with two outcomes.

**The gain change this version nearly shipped was wrong, and the same capture says so.**
A peak of 32,768 at −2.0 dBFS looked like an obvious argument for backing 36 dB off to 27.
Two things in the same reading contradict it:

- the panel was being **held at the owner's face** for a photograph. Ambient in that room
  reads −46 to −54 dBFS, so a voice at the distance this thing is actually used from lands
  near −20 dBFS, which is about where MultiNet wants it. Nine dB down would have put
  room-distance speech at −29 to fix a case that only happens at arm's length;
- and **both successful decodes happened while it was clipping.** "pick a new color" changed
  the colour and "jump up" fired, at −2.0 dBFS. The premise that clipping was preventing
  recognition is contradicted by the only evidence there is for it.

So 36 dB stays, and `MIC_GAIN_FALLBACK_DB` stays at 30 — the two move together or not at all,
which is the actual bug the aborted change surfaced: it briefly left the fallback at 30 while
the primary went to 27, turning "the part refused your setting" into "the part is now
clipping".

The real shape of this is **dynamic range**. Close talk and across-the-room want different
gains, and one fixed number only chooses which end to fail at. That is what AGC is for, and
§10.4ba rejected it because it cost ~103 KB of internal RAM and collapsed the largest free
block to 9,728 — which killed the display, *because the display allocated a DMA buffer per
frame from that heap*. **It does not any more.** `blit_frame()` uses static buffers, so the
number AGC has to survive is no longer the display's. That does not make AGC affordable — Wi-Fi
and TLS still want heap — but it retires the specific reason it was refused, and it is the
first thing to re-measure.

`clipped` now rides in the 3 s report, to measure that trade rather than argue about it. A
peak of 32,768 says the loudest sample hit the rail; it does not say whether one sample did or
ten thousand, and those are a healthy transient and an unrecognisable phrase.

##### The confidence floor is deliberately NOT in this version

The owner said **"turn red"** — which is not in the vocabulary at all, the colour phrases being
"change your color" and "pick a new color" — and the recogniser published `jump up` at
p=0.19. There is no confidence floor in the detect path: `publish()` runs on any
`ESP_MN_STATE_DETECTED` regardless of probability.

A floor obviously belongs there, and it is **not added here**, because nothing in this feature
yet records what a CORRECT decode scores on this hardware. A number picked to reject 0.19
would be picked to reject the only two data points that exist, both of them false accepts, and
if a genuine command also scores low the result is a recogniser that silently stops working
and looks exactly like the microphone being broken again — which is the failure mode this
whole sequence has been made of. So this version logs `p`, the raw decode and the runners-up,
and the floor lands in the next one, from a capture of phrases that are actually in the
vocabulary.

#### 10.4bg The words a four-year-old actually says (0.2.48, 2026-09-21)

The owner, once three commands were confirmed working: *"as far as commands, we need
`turn [color]`, `burp`, `fart`, `dance`, `jump`"*.

Then, immediately after: *"wave, shake, laugh, eat, kick, spin"*. Ten single words in total.

**They break a rule this vocabulary was built on**, and the rule is not wrong.
`vocab.h` rule 2 requires two words because WakeNet is DISABLED — every phrase is always live,
so a one-word vocabulary fires at the television. That is still true. It is relaxed here for
exactly `burp`, `fart`, `dance` and `jump`, because they are what a four-year-old actually
says, and a vocabulary that is safe and unused is not safer.

The allowance is a **named list in the host suite**, not a loosened check:

```c
static const char *const SINGLES[] = {"burp", "fart",  "dance", "jump", "wave",
                                      "shake", "laugh", "eat",   "kick", "spin"};
```

so the next single word has to be argued for rather than slipped in beside these — and the
list grew from four to ten within one version, which is the argument for it being a list. The cost is
paid in false triggers, which makes the missing confidence floor (§10.4bf) more urgent, not
less: a vocabulary that fires more often and cannot say how sure it is fires more often *and*
cannot say how sure it is.

##### Each short form forced a collision

Rule 3 forbids a phrase being a PREFIX of another, because the shorter becomes unreachable and
the longer unreliable. `jump` collides with `jump up`; `dance` collides with `dance with me`.
So do `wave`/`wave hello` and `shake`/`shake your body`. The colliding long forms are reworded
rather than dropped, because two ways to ask is the point of having long forms at all — a
four-year-old says the one you did not think of:

| new short form | long form it collided with | reworded to |
|---|---|---|
| `dance` | `dance with me` | `come and boogie` |
| `wave` | `wave hello` | dropped; `say hello` already covers it |
| `shake` | `shake your body` | `wiggle your body` |
| `jump` | `jump up` | dropped; `bounce around` already covers boing |

`come and boogie` matters more than it looks: bop keeps a voice, and since §10.4bd dance, bop
and shimmy are three different animations on the bird rather than three names for one.
Phrases that merely CONTAIN the word are untouched — `do a dance` and `do a burp` start with
"do" and collide with nothing.

##### Three of the ten did not exist as animations

`wave`, `shake` and `laugh` are new words for actions that were already there. `eat`, `kick`
and `spin` are not.

**`eat` is the clearest case for the bird channels (§10.4bd).** The robot eats with a hand —
three trips to the mouth, because one reads as a mistake and three read as a meal. The ostrich
**pecks the ground**, which is the single most recognisable thing an ostrich does and costs
nothing this rig did not already have: `bob` down 78 px, `neck` forward, `tail` up as the head
goes down. One action, two anatomies, which is exactly what those channels are for.

**`kick` swings one leg, twice, and hard.** Negative `leg_r` swings the foot out to the right
because the limb's x offset is `-sin(deg)`; the other leg stiffens to plant or the figure
reads as falling over, and the arms counterbalance the way a kicking child's do.

**`spin` had to be invented, because `rig.h` deliberately has no rotation:** *"Rotating a
368x448 framebuffer per frame is a per-pixel resample this panel should not spend 25 times a
second, and source-space rotation tears holes in filled shapes... `spin` is left out of the
pools rather than faked badly."* That constraint has not changed.

So the spin is the 2D trick instead: squash the figure horizontally to near nothing and back,
twice, while a new `facing` channel flips — and the renderer **drops the eyes, the smile and
the beak** for the half-turn the figure is away. The squash alone does not read as a turn; a
face that stays put while the body narrows reads as the body being crushed. A silhouette with
no face on it reads as a back. It costs one multiply on a scale the renderer already applies,
and it never squashes past 8% width, because a figure one pixel wide is a gap in the middle of
the screen rather than a character seen edge-on.

Measured, largest change against idle and between consecutive frames:

| | ostrich vs idle | ostrich frame-to-frame | robot vs idle | robot frame-to-frame |
|---|---|---|---|---|
| `eat` | 28,173 | 16,157 | 20,507 | 6,290 |
| `kick` | 30,099 | 9,558 | 32,434 | 6,806 |
| `spin` | 31,817 | 14,224 | **55,515** | 22,303 |

`spin` on the robot is the largest change against idle of any action in the set, which is what
squashing a whole figure does.

##### The palette had no red and no blue

`turn [color]` needs colours that can be NAMED, and this palette was designed as a set of
pleasant tints rather than a set of names. Its nearest to red was `0xFF477E`, which a
four-year-old calls pink; its nearest to blue was `0x6A7BFF`, a periwinkle. **A named colour
command that produces a colour the child would give a different name to is worse than no
command** — and it would be debugged in the microphone, because it presents as mishearing.

So `0xFF3B30` and `0x3B82F6` are **appended**, at indices 11 and 12. Appended is the point:
every index above them is a colour the tap cycle already visits in an approved order, and
inserting would renumber them. The other six map onto entries that were already there and
already look like their name.

`VOCAB_COLOUR` now reads `arg`: below zero steps to the next colour, as "pick a new color"
always did; at or above zero it lands on that palette index. The host suite checks every named
colour is inside the palette, because `turn red` resolving past the end would wrap to some
other colour and look, again, exactly like the recogniser mishearing.

The whole table is now 38 phrases and **84 command words against MultiNet's limit of 200** —
measured from the compiled table rather than counted by eye, after a text scan of `face.c`
miscounted the palette by reading hex values out of a comment.

#### 10.4bh The panel was crash-looping, and the field that would have said so said "other" (0.2.49, 2026-09-21)

The owner, on 0.2.48: *"The screen is black but on power cycling it now."* 0.2.48 carries
0.2.47's buffer fix, so this was never the tearing bug — it is a separate fault, and it had
been reported as "sporadic crashing" for three versions while every investigation went to the
display.

**The console could not see it and structurally never could.** `panel-console` is a live serial
attach: it shows what the panel says from the moment it attaches, so a fault that has already
happened is gone. Worse, attaching resets the chip — which `POST /endpoint/telemetry`'s own
docstring already said in as many words. Every console capture in this sequence was of a
freshly-rebooted panel.

**The telemetry route answered it in one query**, because it logs to the box's API log and the
box was running the whole time:

```
reset_reason "other"  crash_phase  6   uptime 6s
reset_reason "power"  crash_phase -1   uptime 7s    <- the owner's power cycle
reset_reason "other"  crash_phase  6   uptime 6s
reset_reason "other"  crash_phase  9   uptime 7s
reset_reason "other"  crash_phase 10   uptime 6s
```

Uptime never exceeds seven seconds. **The panel is not hanging, it is crash-looping** — dying
and restarting roughly every ninety seconds, and the owner sees the black gap. That is the
whole of "it keeps sporadically crashing", and it was visible on the box for hours.

##### The field whose only job is to name the cause was falling off the end of its own table

```c
static const char *REASONS[] = {"unknown", "power", "ext", "sw", "panic", "int_wdt",
                                "task_wdt", "wdt", "sleep", "brownout", "sdio"};
```

Eleven entries, 0–10. **ESP-IDF's enum runs to 15.** Everything above `sdio` printed `other`,
and `other` is what a crash loop reported for hours. The five missing are `usb`, `jtag`,
`efuse`, `pwr_glitch` and `cpu_lockup` — and `usb` is the prime suspect, because
**ESP_RST_USB is what attaching a serial console to this panel does**: the exact hazard the
telemetry route was built to route around, landing in the one bucket that could not name it.

A diagnosis channel that cannot tell "the firmware crashed" from "someone plugged in a cable"
is worse than no channel, because it invites the wrong fix. It ships now with all sixteen
names **and the raw number beside the name**, so an enum that grows again says so rather than
silently rejoining the bucket that cost a day.

The restart line is also written to the console at boot, not only sent as telemetry. Telemetry
needs the box, the network and the device key; the console needs a cable. Neither is reliable
enough to be the only place a restart is recorded.

##### Where it dies

`crash_phase` is an `RTC_NOINIT_ATTR` breadcrumb, so it survives a reset (though not a power
cycle — hence `-1` on the owner's two). Phases 6, 9 and 10 are `face_draw`, `blit_frame` and
the idle `vTaskDelay`: three unrelated points in the loop, which is the signature of
corruption or an external reset rather than one bad call.

**The obvious suspect is the investigator.** Console attaches were frequent during this
period and each one resets the panel. That is testable for free and without firmware: leave
the panel completely alone and read only the box-side log. Recorded here because the answer
changes what 0.2.50 should be, and because "my own instrument caused the fault I was chasing"
is the fourth distinct instrumentation failure in this sequence, after the memory mode, the
chunk sizes, the AGC mode and the mic gain (§10.4bb).

#### 10.4bi It was the console all along, and the meter becomes a switch (0.2.50, 2026-09-21)

**A large share of the "sporadic crashing" was the investigator.** §10.4bh named `ESP_RST_USB`
as the suspect — attaching a serial console resets this chip — and the test needed no
firmware at all: stop attaching, and read only the box-side log. Telemetry posts once at boot
and then every `CHECK_PERIOD_MS` (15 minutes), so a report at 6–7 s of uptime **is** a boot.

| window | boots |
|---|---|
| 20:55–21:00, console attached repeatedly | **4** |
| 21:01–21:06, panel left completely alone | **0** |

Four reboots in four and a half minutes while being watched, none in five minutes when left
alone. The panel was being reset by the instrument used to investigate why it kept resetting,
and the field that would have said so was printing `other` (§10.4bh). It does not follow that
every black screen the owner saw was this — they reported some with nothing attached — but the
rate was inflated, and 0.2.49 will print `usb(11)` next time so the two can be told apart.

##### The meter becomes a switch rather than furniture

The owner: *"turn the audio meter on the left side to only be rendered if we enable a debug
mode. It's not needed all the time."*

The meter earned its place — `display.c`'s header argues it well, a microphone has no symptom
and a bar that is always running answers "is it hearing anything" at a glance — and it is how
the first successful decode was confirmed to be a voice rather than a number. But bring-up is
over, and what it buys now is a green bar down the edge of a pet in a four-year-old's bedroom.
The right end state for a diagnostic is a switch, not deletion, because the next time the
microphone goes quiet the meter is the fastest answer in the building.

`endpoint_settings.debug_overlay`, off by default: a debug overlay that defaults on is one
nobody turns off. The absent case in the firmware's parser means OFF rather than "unchanged",
or a panel that once had it on keeps it forever and the switch works in one direction only.

**AND THE KNOBS HAD NO HANDLE.** `0206_endpoint_settings` moved volume, mic gain and
brightness out of firmware constants specifically so the owner could change them without a
build — and then nothing was ever built to change them *with*. No PWA screen, no command in
`debug-connect.sh`, nothing. They have been settable in principle and unreachable in practice
since the day they shipped, which is exactly the terminal dependency `CLAUDE.md` #10 exists to
design out rather than an inconvenience to note. `scripts/debug-connect.sh panel-settings`
closes it for all four; a PWA control is still owed.

##### The one setting on this chip nobody had ever read

The owner: *"I don't know if Gain is automatic but it seems like when it beeps that it kind of
rails the audio gain meter for 4 to 5 seconds after it beeps."*

Two candidates, and this firmware could answer for neither:

- **The ES8311 has its own ALC** — an automatic gain control in the codec, ahead of anything
  the firmware can see. `es8311.c` writes REG1B and REG1C (automute, HPF) and **never writes
  REG18, the register that enables it.** So ALC has been at the chip's reset default since the
  first bring-up, and seconds of gain ramp after a loud sound is exactly what an ALC release
  does. The front end's own AGC was already off (§10.4bb) — a different knob entirely, and
  checking it proved nothing about this one.
- **`es8311.c` also sets REG44 = 0x58**, which the driver's own comment calls the "internal
  reference signal (ADCL + DACR)": the DAC deliberately routed into the ADC. **The panel is
  wired to hear its own speaker.**

`alc_settle()` reads REG18, logs what it actually was, clears the enable bit while preserving
the window size, and **reads it back** — `0xXX -> 0xXX (off)` or `STILL ON`. It runs from the
audio task, because that task owns the codec and a register poke from anywhere else is the
race that panicked a panel (§10.4al).

The memory mode, the chunk sizes, the AGC mode and the mic gain were each a value this
firmware set and never read back, and every wrong diagnosis in this sequence traced to that
(§10.4bb). This is the fifth, and it was never set at all — which is worse, because a default
nobody chose is indistinguishable from a decision until someone reads the register.

Separately and regardless of which it is: **the panel now stops listening to itself.** Six
chunks of deafness after the speaker runs, covering the 90 ms beep and a tail. Feeding our own
tone to the recogniser is not merely noise, it is a false trigger with a loudspeaker behind it.

#### 10.4bj A quarter turn is not a rotation (0.2.51, 2026-09-21)

The owner: *"Can we also have the capability of having it 90 degrees out so if it came from
the cable and it's 90 the bottom is lower? You'd have to scale the bird and all of this but I
think it would be beneficial to the usability for the twins."*

**`rig.h` refuses to rotate the figure, and that reasoning still holds — but it does not apply
here.** What it rejects is an *arbitrary* angle: a per-pixel resample this panel cannot spend
25 times a second, and which in source space tears holes in filled shapes. A quarter turn is
neither. It is an **index permutation** — every destination pixel is exactly one source pixel,
no interpolation, no gaps — the same class of operation as the 180° flip this panel has done
since 0.2.19.

##### The square is what makes it free

A quarter turn maps a **square** onto itself. So the figure renders into a 368x368 region of
the 368x448 frame and the rotated blit is source-square to destination-square: no second
framebuffer, no reallocation, the same two 11,776-byte stripe buffers. The 40 px above and
below are never written, and on an AMOLED an unwritten pixel is an unlit one, so the bars are
invisible rather than grey.

**And the direction of the scan is the whole performance story.** The obvious loop reads the
source across a row and writes down a column, which on a framebuffer in PSRAM is 368 cache
misses per stripe. Blitting **column** stripes instead — `esp_lcd_panel_draw_bitmap` takes any
rectangle, not only full-width bands — inverts it: for a fixed destination column the source
addresses are consecutive, so PSRAM is read sequentially and the scattered writes land in
internal SRAM where a stride costs nothing.

##### Four ways up, from the two axes the flip already used

Gravity on X is portrait and its sign says which way up; gravity on Y is landscape — mounted
with the cable out the side — and its sign says which. Whichever axis is larger wins, with the
same half-a-gravity hysteresis the two-way version needed, because a panel lying near flat has
almost nothing on either axis and a bare comparison would flip back and forth on noise.

##### The scale, which the owner called before it was measured

The figure is composed for 448 of height and is 428 px of it. On its side it has 368, so
everything scales by 368/448 through one scalar (`face_set_fit`) rather than a second
hand-tuned layout — every number in `face.c` was measured against the mock, and a second set
would be a second thing to keep true.

**The first version of the test demanded no clipping at all, and failed.** Measured:

| scale | poses clipped (of 646) |
|---|---|
| portrait, 1.00 | **0** |
| square, 0.82 | 76 (11.8%) |
| square, 0.78 | 56 (8.7%) |
| square, 0.66 | 0 |

Reaching zero needs **0.66 — a third smaller than portrait**, and §10.4at already rejected
that trade for the portrait figure in almost the same words: *"shrinking the approved bird by
a sixth to save an average of four pixels a frame, on a figure already 428 px tall in a 448 px
panel, is the wrong trade on a 29 mm screen; a cropped toe at the peak of a gag reads as
energy."* Shrinking by a third to save a crest tip during a boing is the same trade and worse.

So the test asks two different questions instead of one: **nothing clips at rest** — a pet
cropped while standing still is simply drawn wrong — and clipping across all poses stays
inside the 15% §10.4at accepted for portrait. It lands at 11.8%.

##### What a quarter turn would have silently eaten

Both overlays sat outside the square: the version label at y=6 is above it, and the caption is
anchored to the bottom of the frame and is below it. The rotation would simply not have
carried them, so the first thing lost on a side-mounted panel would have been **the caption —
the one piece of feedback that says a command was heard**. Both take the square's bounds now
instead of the frame's.

The permutation is checked on the host rather than reasoned about: turning one way then the
other is the identity across every pixel of the square, and a named corner is asserted by hand,
because "it round-trips" is also true of doing nothing. An off-by-one in a permutation is a
mirrored pet, which looks deliberate.

#### 10.4bk Press, hold, and a box that thinks (0.2.52, 2026-09-21)

The owner, specifying the conversation gesture: *"when we long press ... it should make a
[sound] when it activates the listening and then when we release it should show the thinking
box."*

This ships **the interaction and nothing behind it yet**, deliberately. The gesture, the
sound, the listening state, the thinking box and the failure state are all panel-side and cost
nothing to get right first; the round trip is gated on a measurement nobody has taken (below).

##### The hold threshold is the whole design problem

`gesture.h` records that 4–5 year olds produce ordinary presses lasting **up to 4.2 s**, which
is exactly why the maintenance gestures stopped being a bare hold. A talk gesture cannot wait
4.2 s — nobody holds a button that long before speaking — so it fires at **700 ms**, past the
600 ms that still counts as a tap, and accepts that ordinary play will sometimes start a
listen.

**That is survivable here in a way it was not for "reboot the panel".** The cost of a false
listen is a beep and a discarded recording; the cost of a false reboot is a toy restarting in
a child's hands. Same measurement, opposite conclusion, because the consequences are not
comparable.

It never fires mid-maintenance-gesture: those are taps *then* a hold, so a hold beginning
while a tap run is live belongs to them (`gest.taps == 0` is checked after `gesture_poll`).

##### Four states, and the fourth is the one that matters

| state | what shows |
|---|---|
| listening | **a beep**, a pulsing red dot, and the pet wears `FACE_CURIOUS` |
| thinking | the bubble, three dots filling in turn |
| failed | the bubble in grey with a flat red dash, and `FACE_BEWILDERED` |
| idle | the pet, as before |

**The beep is the affordance.** Nothing else tells a child holding a 29 mm screen that the
thing is now listening rather than merely being held.

**And the failure state is not optional.** After 12 s with no reply the panel says so rather
than returning quietly to idle, because on a device whose owner has no terminal *"it didn't
hear you"* and *"it is broken"* must not look identical — which is the exact failure mode
§10.4bc was written about. The dash is a different SHAPE from the dots, not merely a different
colour, so the two read apart at a glance on a screen this small.

The face follows the conversation: attentive while listening, bewildered on failure. A pet
that keeps grinning through a failure is a pet that looks like it did not notice.

##### What is not here, and why

The capture buffer, the upload and the reply. `../proposed/PANEL_CONVERSATION_PLAN.md` records
the reason: **whisper takes ~9.8 s per call on this box**, flat, because whisper.cpp pads
every clip to a 30-second window (`api/sdr.py:1412-1416`). No thinking box covers nine
seconds. `scripts/whisper-setup.sh:5` already makes the model operator-selectable and
`base.en` is an option — **that measurement is the next step, and it decides whether the round
trip is a week of firmware or a different STT entirely.** Building the transport first would be
building on a number nobody has.

#### 10.4bl The help text was executable (2026-09-21)

0.2.50 added two usage examples to `scripts/debug-connect.sh`. **Without `#` prefixes.**

`usage()` prints the script's leading comment block —
`awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"` — a nice trick with a
sharp edge: a help line that is not a comment is not documentation, it is **executable bash at
the top of the script**. Both offending lines read `scripts/debug-connect.sh panel-settings
...`, so every invocation immediately re-ran the script:

```
+ scripts/debug-connect.sh panel-settings
bash: warning: shell level (1000) too high, resetting to 1   (x1000)
```

Infinite recursion, and a hang with no output — `bash -x` produced nothing because the trace
died with the pipe. It also silently truncated the help at the first bad line.

**It broke a deploy before anyone noticed.** The 0.2.50 rollout was attributed to a container
restart killing the watcher; the box was still on 0.2.48 long afterwards because every
`debug-connect.sh update` had been hanging on this. That script is how the owner updates a box
they cannot open a terminal on (`CLAUDE.md` #10), so this broke **the recovery path as well as
the thing being recovered** — the worst shape a bug can have on this product.

##### The first guard passed with the bug put back

```python
if not line.startswith("#"):
    assert i > 10, ...      # "the help block is long enough"
    break
```

The offending line is line 14, so `i > 10` held and the test broke out happily. It asserted
the block was long enough rather than that nothing in it runs — a test that passes for the
wrong reason, which is the same failure this feature has produced four times now (§10.4at's
`test_every_action_moves`, and the two neck-shear tests in §10.4bd).

The one that ships **runs the script** and requires it to terminate, because a recursion is
perfectly valid bash and only behaviour catches it, with a lexical check beside it so the
failure names the cause instead of just timing out. Confirmed to fail on the real defect and
pass once fixed, in that order.

#### 10.4bm The subtitle bar was a hole in the picture (0.2.53, 2026-09-21)

The owner: *"the text scrolling on the bottom going from right to left, the black in. It
should be transparent."*

The ticker cleared a full-width black strip before drawing, and **the reason was real and
measured**: the ostrich's feet reach y=435 on a 448 px panel while the ticker runs at 432, so
"PLAY PEEKABOO" ran straight through its toes. The bar is what made the words legible over
whatever the pet was doing.

But on an AMOLED a cleared row is **off**. That was not a tint over the pet, it was a
hard-edged hole punched through its feet, on a toy — and the owner looks at this thing all
day.

So the legibility moves from the background to the glyphs, the way subtitles have always done
it: the text is drawn four times in unlit black, offset two pixels each way, then once in its
own colour on top. Every pixel *between* the letters is untouched, so the pet shows through
and the words stay readable over legs, wings or nothing. Five draws rather than one, on a
short string, at the five frames a second the face actually redraws.

**Measured, because "it looks transparent" is not a property a test can hold:** the figure's
own lit pixels inside the ticker's band, with and without a caption drawn over them.

| | lit pixels in the band |
|---|---|
| figure alone | 330 |
| figure + caption (halo) | **359** — the feet survive, the letters add |
| figure + caption (old strip) | the letters alone; all 330 erased |

The test asserts the figure keeps at least nine tenths of its pixels through a caption, which
allows the halo to erode a few where a letter sits on a toe — that erosion is the point of it
— and forbids wholesale erasure. **Confirmed to fail with the black strip put back**, then
pass with it gone, in that order; four tests in this feature have now passed for the wrong
reason, and checking the direction costs one minute.

#### 10.4bn Off the cable, and every diagnostic went with it (0.2.54, 2026-09-21)

The owner: *"I moved to not be on USB."*

That is the product working as designed — §10 has always said the box's USB port is for the
first flash only and every update after it arrives over Wi-Fi — and it removes two fault
sources at a stroke: the `ESP_RST_USB` resets a console attach causes (§10.4bh), and the
power cycles a box update inflicts on a panel drawing power from it.

**And it silently invalidated every instrument added today.** The ALC register reading, the
render heartbeat, the restart line: all `ESP_LOG`, and an `ESP_LOG` exists only on a serial
console. The panel no longer has one, and never will again in its real place. The single
question the owner actually asked — is the codec's automatic gain railing the microphone
after a beep — was being written to a wire that is not connected.

This is the same mistake as §10.4bh in a new costume. There the console could not see a fault
that had already happened; here it cannot see anything at all. Both times the channel that
worked was `POST /endpoint/telemetry`, whose docstring said so in advance: *"the owner moving
one to a plain USB charger, which is the whole premise, must not cost the ability to see what
it is doing."*

So the answers move to the channel that survives:

| field | what it settles |
|---|---|
| `alc` | `"f8-78 off"`, `"78 already-off"`, `"REFUSED"` — the ES8311's gain register before and after, read back |
| `blit_ok` / `blit_fail` | frames that reached the glass and frames that did not |

`TelemetryIn` is deliberately a flat bag of short strings and ints — *"a migration per question
would mean the question does not get asked"* — so this costs two fields and no schema change.

**The general rule this sequence keeps re-teaching:** a diagnostic is only worth what its
channel can carry. Five values were set and never read back (§10.4bb, §10.4bi); two channels
were trusted past what they could see (§10.4bh, here). Every wrong turn in this feature has
been one of those two shapes.

#### 10.4bo The box half of the conversation (0.2.55, 2026-09-21)

The owner: *"let's go ahead and wire things in same flow as the jpet"* — and, on the prompt,
*"probably want a different prompt though. Started off very easy. Just a generic conversation
prompt."*

##### Why the jpet's flow is the right one to copy

`api/pet.py:_say` already solved the part that is not the model. A fast keyword classifier
runs FIRST, so colours and actions never reach an LLM at all; only open-ended input does, and
that leg is wrapped so that a slow, unconfigured or broken model **degrades to a canned line
rather than a 500**. That is the property a pet in a child's bedroom needs: a toy that goes
quiet while a container restarts is indistinguishable from a toy that is broken.

`POST /endpoint/converse` takes the same shape. `PanelDep`, so a panel talks with the device
key it already uses for its manifest and telemetry. Nothing is stored — no memories, no
domain — which keeps a stolen panel key worth exactly one conversation.

##### But not the jpet's prompt

`jpet/brain.py:_system_prompt` is built around wall objects, scene effects and an action
script schema. None of that exists on a panel, and inheriting it would have the pet narrating
furniture a child cannot see. So the panel gets the smallest thing that behaves: who it is,
who it is talking to, and the one constraint that is not a style preference —

> *Your reply is read aloud, so write only what should be said.*

Length is latency here. Every extra sentence is a second the child stands there holding a
29 mm screen.

##### Raw PCM in, raw PCM out

16 kHz mono s16 both ways, which is what the panel captures and what it can play. **Every
conversion happens on the box**: it has CPU to spare and the panel has 31 KB of contiguous
internal RAM. No decoder, no resampler, no WAV parser in firmware — the single decision that
keeps the firmware side small.

The WAV coming back from Kokoro is **walked, not skipped**. A fixed 44-byte offset is the bug
that ships as a burst of noise before every reply, because a WAV may carry LIST/INFO chunks
before `data` and Kokoro's layout is not promised. There is a test with a spliced LIST chunk
for exactly that.

##### And every turn logs the three numbers that decide whether this is usable

`stt_ms`, `llm_ms`, `tts_ms`. Whisper was measured at ~9.8 s with the large model
(§10.4bf) and no thinking animation covers that. Wiring the route up is how the real number
arrives — from the room, on real speech, rather than from a bench.

##### Two tests that were asserting nothing

- The auth test passed a bogus bearer token and expected 401, and got 200. `PanelDep` accepts
  the owner's session cookie **or** a panel key, deliberately, and the fixture logs in as the
  owner — so the cookie was answering and the bearer was never consulted. It clears the cookie
  now.
- The TTS fake returned a detached `httpx.Response`, and `raise_for_status()` refuses to judge
  a response that was never sent — it raises `RuntimeError`, not `HTTPStatusError`, so the
  route's own error handling was never exercised.

##### The hold threshold was already 700 ms, and drifted anyway

The owner asked for "one second, maybe even 3/4" and `HOLD_TALK_MS` was already 700. But the
firmware counted `s_down_ms += TOUCH_POLL_MS` once per loop pass, which silently assumes the
loop runs every 40 ms — it does not. The delay is 40 ms and *then* the frame's work happens,
so a tally of nominal ticks always lags the wall clock and the hold took longer than the
700 ms it claimed. It is a timestamp now, which cannot drift.

**Still not wired: the panel end.** Capture, upload and playback. The box will answer a
`/endpoint/converse` today; nothing on the panel calls it yet.

#### 10.4bp The meter was drawn twice and switched once (0.2.56, 2026-09-21)

The owner, on 0.2.53: *"I'm on 5'3 but the mic meter is still here. Maybe it's the old version
there?"*

Not the old version. **The meter is drawn twice, and 0.2.50 gated one of them.**

Both exist on purpose. `draw_meter()` paints it into the frame, so it survives a full repaint;
`blit_meter()` pushes it as its own narrow strip, so it can update at 25 fps while the face
redraws at 5 — that second path is §10.4's fix for a bar that lagged the room. The debug
switch went on `blit_meter` alone, so the bar kept being painted into every face frame and the
setting appeared to do nothing.

The tell in how this happened is worth keeping: `blit_meter` is the one with the interesting
comment attached — the one that comes to mind when someone thinks "the meter". The other is
four lines in the middle of the draw list. **A feature with two draw sites needs the condition
at both**, and "I changed the meter" read as done because only one of them was in view.

It cannot be caught by the host suite either: `display.c` is full of ESP headers and is not in
that build, which is why the renderer's own `face.c` is tested to the pixel and this is not.
Recorded rather than papered over — the check lives in two places now and the comment at each
says why there are two.

#### 10.4bq The panel starts recording (0.2.57, 2026-09-21)

The first half of the panel's side of a conversation: a hold now **captures audio**, into a
buffer claimed once at start-up out of PSRAM.

**Claimed once, never during a recording.** 6 s of 16 kHz mono s16 is 192 KB, out of the
7.8 MB of PSRAM nothing else wants — and a heap request in the middle of a four-year-old
talking is a failure with no good outcome. Allocation failure is not fatal either: the panel
keeps its voice commands and its meter and simply cannot record, which is a smaller loss than
refusing to start.

**It stops at the cap rather than wrapping.** A ring buffer would hand the box the END of a
long hold, and what a child said is at the start.

**The recording opens AFTER the beep, deliberately.** `audio.c` goes deaf for six chunks once
the speaker runs (§10.4bi), so opening it here keeps the panel's own tone out of the front of
every message — the same fault that would otherwise have the recogniser transcribing a beep.

**And it reads the chunk the recogniser already gets.** One microphone, one owner, one read.
A second reader would be the two-owners fault that panicked a panel earlier today (§10.4al),
and the capture is a `memcpy` inside the task that already owns the codec.

##### Held and captured are different numbers

```
talk: held 1840 ms, captured 1640 ms (52480 bytes)
```

Printing only the first is how a dead microphone looks like a working one. They diverge when
the buffer failed to allocate, when the six-second cap bites, and when the deaf window after
the beep eats the start — three different bugs that a single number cannot tell apart. This is
the same lesson as §10.4bb, applied before it costs anything rather than after.

**Still to come: the upload and the playback.** The box has answered `/endpoint/converse`
since §10.4bo; nothing calls it yet. The upload must not run on the render task — a network
round trip there is a frozen pet — so it wants its own task, which is the next piece.

#### 10.4br The loop closes (0.2.58, 2026-09-21)

Hold, talk, let go, and the panel uploads the recording, waits, and **speaks the reply out
loud**. The last piece of the owner's press-and-hold.

##### Its own task, and that is the whole reason `talk.c` exists

A turn is an HTTPS round trip that can take seconds — whisper alone was measured at ~9.8 s
with the large model (§10.4bf). Doing it on the render task would freeze the pet for the
entire wait, which is precisely what the thinking bubble is there to prevent: **an animation
that stops animating is worse than no animation, because it reads as a crash.** So the
renderer hands over a recording and asks a question every frame (`talk_state()`), and never
blocks.

Pinned off the render core, because it sits on a socket for seconds at a time.

##### One chunk per pass, not the whole reply

`esp_codec_dev_write` blocks. Handing it two seconds of audio would stop the audio task — and
that task's read is the clock for the level meter, the recogniser *and* the capture. A chunk
at a time keeps the loop turning, and lets a reboot or an OTA interrupt a reply rather than
wedging the panel until it finishes talking.

The panel also goes deaf while it speaks. The codec routes the DAC into the ADC by design
(§10.4bi), so **everything the pet says, it also hears** — and feeding that to the recogniser
would have it answering itself.

##### Three states the renderer can leave THINKING through, and only one is a failure

| the box said | what happens |
|---|---|
| 200 with audio | the pet nods and speaks; state held on `audio_playing()`, not a timer, so a long reply cannot end on screen mid-sentence |
| 204, heard nothing | straight back to idle — an accidental hold on a quiet room is the most common recording this will ever make, and it is not an error |
| anything else, or 12 s | the failure face |

A recording that never started, or a hold while a turn is already in flight, goes back to idle
**without** showing a bubble. A thinking box with nothing behind it is exactly the silent hang
this state machine exists to avoid.

##### The compiler found a real one

```
talk.c:113: error: 'sent' may be used uninitialized [-Werror=maybe-uninitialized]
```

Every `goto done` on an early failure jumps past that assignment, and the log at the bottom
reads it regardless — so a failed connect would have printed an upload time made of stack
garbage, in the one line added to diagnose slow turns. `-Werror=maybe-uninitialized` earned
its place.

##### And the timing splits at the right seam

`turn:` logs bytes sent, **upload ms** and **total ms** separately, because the upload is the
network and the rest is the box. "It took nine seconds" says nothing about which end to fix.
The box's own `endpoint.converse` line carries the other half — `stt_ms`, `llm_ms`, `tts_ms` —
and the two together account for the whole wait with nothing unexplained between them.

**This is how the whisper question finally gets answered**: from a child's bedroom, on real
speech, rather than from a bench.

#### 10.4bs A five-second window on the angriest path (0.2.59, 2026-09-21)

Found by re-reading 0.2.58 rather than by running it, which is the only way this one was ever
going to be found.

`talk.c` uploads **straight out of the capture buffer** — no copy, deliberately, because a
second 192 KB buffer to hold a copy of the first is 192 KB spent on nothing. The lifetime rule
that makes that safe is "the buffer is not reused until the next `audio_capture_open()`".

**Two timeouts broke it.** The renderer gives up at 12 s and shows the failure face, holds it
for 2.5 s, then returns to idle. `talk.c`'s HTTP timeout is 20 s. So for about five seconds
the socket is still reading the buffer while the state machine is perfectly willing to start a
new recording into it.

And it is not an exotic path. It is what happens when the box is slow and **a four-year-old
holds the panel again because nothing happened** — the single most likely human response to a
failure face, arriving in exactly the window where the bytes are still in use.

The fix is one condition: a hold cannot start listening while `talk_state()` is BUSY. The
alternative — copying the recording for the upload — buys nothing and costs a fifth of a
megabyte.

Worth recording because of *how* it was found. The build was clean, the host suite was green,
and the panel would have worked every time anyone tested it deliberately; the failure needs a
slow box and an impatient child, together. Reading one's own diff for lifetimes is not a
substitute for tests, but it is the only thing that catches a race whose trigger is someone
being annoyed.

#### 10.4bt A truncated credential is a sentence that sends you to the wrong end (0.2.60, 2026-09-21)

`talk.c` built its bearer header into a fixed 192-byte buffer. The token comes out of NVS with
no length bound, and `snprintf` **truncates silently** — so a long enough token would produce a
valid-looking header carrying half a credential, the box would answer 401, and the panel would
show a failure face.

`ota.c` had already got this right, with `malloc(strlen(token) + 8)`.

The size is not really the point. The point is what the failure would have *said*: "the box
rejected me" sends the next person to the server to look at authentication, and the fault is
two files away on the device. Both lengths are checked now and a truncation names itself
before anything is sent.

This is the same shape as every other fault in this sequence — five values set and never read
back (§10.4bb, §10.4bi), two channels trusted past what they could see (§10.4bh, §10.4bn) —
and it is the reason for the rule those keep pointing at: **an operation whose failure cannot
be distinguished from a different failure is not finished.** `snprintf` returning a number
nobody looks at is exactly that, in one line.

#### 10.4bu The lean was pinned, not dead (0.2.61, 2026-09-21)

The owner, on the side-mounted panel: *"went in the horizontal position. It doesn't tilt
inside to side as expected."*

`update_orientation` read the lean off `-ay` unconditionally. That was right for the only two
orientations that existed when it was written, and **wrong the instant the panel is on its
side**: in landscape `ay` is the axis carrying GRAVITY — about 8,000 against a `LEAN_FULL` of
2,400 — so the target clamps to `LEAN_MAX` and the figure sits at full lean, permanently.

Not a dead sensor. A saturated one. Those look identical from across a room and have opposite
fixes, which is the whole reason this is worth a section.

##### The mapping is derivable, not guessed

Upright, gravity is on +X (§10.4 established that from a reading taken in a *known* pose,
after the first attempt guessed from an unknown one and shipped backwards), and the lean was
`-ay` — so the viewer's right is board −Y. Turn the panel a quarter clockwise and what pointed
right now points down, so viewer-right becomes board −X. Each further quarter walks the same
circle:

| | viewer's right | lean from |
|---|---|---|
| upright | −Y | `-ay` |
| clockwise | −X | `-ax` |
| upside down | +Y | `+ay` |
| anticlockwise | +X | `+ax` |

Which is also why the magnitudes come out right: **whichever axis is not carrying gravity is
the small signal a tilt moves**, in every orientation. The saturation was the symptom of
reading the wrong one, and the table fixes both at once.

##### Two reports in the same photo that were not new bugs

The owner also saw the microphone meter still drawn, and a column of banded artefacts down the
left edge. The panel was on 0.2.53; the meter's second draw site was fixed in 0.2.56
(§10.4bp), which had not reached it. And the banding **is** that same fault: `draw_meter`
paints the bar into the frame at 5 fps while `blit_meter` pushes its own strip at 25, so
between repaints the two show different levels and the column bands. One fix, already in
flight, for what looked like two problems and a tearing bug.

#### 10.4at Four actions that posed but never performed (2026-09-21)

A code researcher was sent over `face.c` after the ostrich landed. Rather than take the report,
I built a harness that **renders frames and counts changed pixels**, and checked each claim
against it. Two of the report's headline numbers were wrong in my favour and one in the other
direction; the measurement is what this section records, not the report.

| Action | Measured before | What was wrong |
|---|---|---|
| `blush` | **0 px on the robot**, 1525 on the ostrich | drawn before the body and head, which then painted over it. On the ostrich the anchor was the head's y and a fixed ±78 in x — a head 128 wide, not 216 — so both cheeks landed in **empty space beside the bird**. |
| `wave` | **0 px on the ostrich**, 3499 on the robot | the ostrich's tail flap was driven from `arm_l`; `wave` only moves `arm_r`. On the robot the arm posed at −150° put the hand at x+103 against a head reaching x+108 — **hidden behind the face**. |
| `hide` | robot −35%, ostrich **−13%** of eye white | the robot's hand knob is 17 px against a 41×48 eye; the ostrich's wing was 74 px wide, parked at the eye line, and **drawn before the head**. |
| eye scale | 0.98 | transcribed literally from the mock, whose eye base is 46×54 where `draw_eye`'s is 40.6×48.4. The approved eyes shipped **a ninth too small**; 1.10 reproduces them to within half a pixel. |

**Every one of these passed the whole test suite,** including a test named
`test_every_action_moves`. That test compares `rig_pose_t` and `figure_pose_t` — and all four
defects pose correctly. `wave` moves a float. `blush` sets `fig.extra`. `hide` drives
`hands_up` to 1.0. The pose is right in every case and **nothing reaches the glass**.

So the suite now renders. `test_every_action_changes_the_picture` requires ≥900 changed pixels
for every action on every form; `test_peekaboo_covers_the_eyes` requires **zero** eye-white
mid-hide on both forms; `test_the_blush_lands_on_the_face` requires the pink to survive the
frame *and* to sit within the eyes' band; `test_the_gag_puff_shows_on_both_forms` counts the
cloud's unique colour. Each was confirmed to fail against the pre-fix renderer before being
kept — the delta test and the peekaboo test name the exact symptom.

The fixes are mostly **ordering**, which is the tell: the puff belongs behind the figure and
the blush on the face, so they became `draw_puff()` and `draw_blush()` called at per-form
anchors rather than one function called before everything. The ostrich's wing is drawn behind
the body at rest and **over the head** once it is hiding, and grows 74→144 px as it rises. The
robot's peekaboo hand grows 18→32 px so it can actually cover the eye it is aimed at, and an
arm posed above the shoulder is **redrawn over the head** — the shoulder sits at ox+66 inside
a head 216 wide, so a raised arm has 42 px of head to clear and otherwise disappears. `wave`
swings −106°…−154° to stay wholly past that threshold, so the arm cannot flick in front and
behind between frames.

**Clipping was measured and deliberately left alone.** Across 425 sampled frames per form, 62
(15%) on the ostrich and 43 on the robot lose pixels off a panel edge — crest tips and toes
during the biggest squash-and-stretch. A sweep of base scale × origin says it only reaches zero
at **k=0.83** for the ostrich. Shrinking the approved bird by a sixth to save an average of
four pixels a frame, on a figure already 428 px tall in a 448 px panel, is the wrong trade on a
29 mm screen; a cropped toe at the peak of a gag reads as energy. Recorded here so the next
reader knows it was measured, not missed.

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

#### 10.4bv One buffer, two frames, and a corner that never got the message (0.2.62, 2026-09-22)

The owner, on 0.2.61: *"mic bar is gone, but when horizontal, we now have some weird screen
artifacts on the left side, and I'm not quite sure why there's a red dot when I'm horizontal on
the bottom left. What does that indicate?"*

Three separate things, and the interesting one is not the one that looks like a display bug.

##### The artifacts: the double buffer had a seam at every frame boundary

§10.4bf bought two stripe buffers and a one-deep transfer queue, and the comment there states
the invariant exactly: *"at most ONE is ever in flight when we return — and the stripe we are
about to fill is by definition the other one."* True within a blit. Both blits then opened with
a **local** `bool odd = false`, so the alternation restarted every frame — and "by definition
the other one" quietly stopped being true across the boundary.

Portrait survived on arithmetic. 448 / 16 = **28** transfers, an even count, so a frame ends on
`stripe_b` and the next begins on `stripe`. The rotated blit does 368 / 16 = **23**, an odd
count: every landscape frame ended on `stripe` and the next frame's first `memcpy` wrote over
the transfer still sending it. One 16 px column of glass, assembled from two different frames,
25 times a second, for as long as the panel is held sideways.

The one-shot clear on the turn had the same hole and left a permanent mark rather than a
flickering one. It queues 28 stripes of zeros over the whole panel, returns with the last still
in flight, and `blit_frame_rotated` then fills a buffer whose size is `SQ * COL_STRIPE` = 5888
pixels — **exactly `sizeof(stripe)`**. So the rotated figure landed on top of the zeros the DMA
was reading, and panel rows 432–447 came out as transposed garbage. Nothing rewrites that
region until the next turn, so it stays.

Both are the same defect, and the fix is one line of state: the toggle moves to file scope and
is never reset. Three loops share it now; none may assume where the previous one stopped.

**What this cost to find, and why.** The first hypothesis was the overlays — the cue bar and
the tap pips still draw at rows 0–3, which a quarter turn does not carry. That is a real defect
and is fixed here too, but it is the **opposite** symptom: content that lands outside the square
is *invisible* in landscape, never corrupt. Checking which rows the rotated blit actually reads
(`SQ_Y0` … `SQ_Y0 + SQ`) ruled it out in a minute and pointed at the only other thing that
writes those rows — the clear — and from there at the buffer it writes them with.

##### The red dot: it is not an artifact, and the panel should not have been listening

`draw_listening`. The recording indicator, top-right of the square, which a quarter turn puts in
a corner — as designed. The answer to *"what does that indicate?"* is **the panel thought it was
being held to talk**, because it was being held.

Two things follow.

The dot needed a word. Its comment claimed it was *"the one symbol for recording that needs no
explaining"*, and then the person who specified the feature photographed it and asked what it
meant. A red dot reads as recording next to a camera; on a pet's face it reads as part of the
pet. The dot stays for the twins, who cannot read it. `LISTENING` under it is for whoever has to
work out why the panel is doing something — and it is the fastest way to spot a listen nobody
started.

And 700 ms is short enough that **carrying the panel starts a recording**. §10.4bn waved this
through — *"the cost of a false listen is a beep and a discarded recording"* — which was true of
the state machine and stopped being true when `talk.c` landed behind it. A false listen now
uploads six seconds of a child's bedroom and makes the pet answer something nobody asked.

The discriminator was already computed. A hand carrying a 32 mm panel touches its **rim**; a
press meant for the pet lands on the pet, which occupies the middle. The hold must now begin
inside a 72 px inset — about 6 mm, roughly the half-width of an adult thumb pad — leaving a
224 × 304 target a four-year-old cannot miss. In **panel** coordinates, so the rim is the rim
whichever way up the unit is mounted, and sampled at the down edge rather than read at the
threshold, because `s_tap_x` outlives its press and a finger already down when the loop starts
would otherwise inherit the last one's position. A rejected hold logs once, with the
coordinates: the owner has no terminal, and a margin that is too wide looks exactly like a
microphone that stopped working unless the panel says which it is.

This is a margin, not a cure. Grip contact that lands squarely on the pet's face still fires.
The complete answer is the rhythm in `gesture.h` — slots 1 and 2 are free, and it measured 12
false fires per 20,000 simulated child presses against a bare hold's 1,937 — but a rhythm is a
thing to teach, and the owner asked for press-and-hold. Teach it only if the margin is not
enough.

##### Still unverified

Press-and-hold has **never been exercised end to end on hardware**, so the whisper round trip
from the room is still a number nobody has. The margin makes the gesture harder to trigger by
accident; whether it is still easy to trigger on purpose is a question only the twins answer.

#### 10.4bw The finger was in the wrong space, and so was my answer (0.2.63, 2026-09-22)

The owner, correcting §10.4bv: *"No, the listening is on the top right. Also in landscape he
should be able to tilt and slide all over to the right and I'll put it to the left, not
restrained as much. Also, while horizontal the touch screen indicators do not indicate where I
actually tapped — it's like rotated 90° or something."*

**The red dot at the bottom left was never `draw_listening`.** The listening dot is top right,
where it is drawn. What was at the bottom left was the **tap marker** — the amber ring that
rides the flinch — landing a quarter turn away from the finger that made it. §10.4bv's
explanation was confident and wrong, and it was wrong in the way worth recording: it explained
a symptom with the most recently changed code rather than checking which code draws the shape
that was actually photographed. The margin it added to the talk gesture is still right for its
own reasons; the diagnosis it was attached to was not.

##### Panel coordinates are not frame coordinates

The touch controller reports where a finger is on the **glass**. The figure is drawn in
**frame** coordinates and permuted into panel coordinates by `blit_frame_rotated` on the way
out. Everything downstream of a finger read the first as if it were the second, so on a
side-mounted panel:

- the tap marker was drawn into the frame at the glass position, and the blit then carried it a
  quarter turn away — the owner's rotated indicator, and the bottom-left dot;
- `face_zone` was asked where on the figure the finger landed using a point that is not in the
  figure's space, so poking the bird's neck answered as a leg. **Nothing said so.** A wrong
  reaction and a right one look alike from across a room, which is why this half of the defect
  could have lived indefinitely.

`panel_to_frame()` inverts the blit's mapping. Upright is the identity; upside down is *also*
the identity here, because `flip_frame` reverses the whole buffer after the marker is drawn and
the panel is then physically turned over — the same two-reversals-cancel argument §10.4bu had
to get right for the lean, and the same one that is easy to talk yourself out of. What stays in
panel coordinates: the calibration map, the telemetry, and 0.2.62's talk margin, because the
rim of the glass is the rim of the glass whichever way up the unit is mounted.

`face_zone` had a second, quieter fault in the same family: it ignored `s_fit` and `s_fit_oy`
entirely, so side-mounted it asked about a figure a sixth larger than the one on the glass. It
now inverts the same transform `face_draw` applies, and the test below measured the old code
disagreeing with itself on **30% of a sampled grid**.

The tap log now prints both pairs. A tap the glass and the figure place differently is a
rotation fault; one they place identically but in the wrong zone is a calibration fault; the
owner has no terminal to tell those apart with.

##### The lean: 110, and it is a measurement

*"Not restrained as much"* is a number, so it was measured rather than chosen. Walk the lean
until a form's bounding box touches an edge, under each fit:

| | ostrich | robot |
|---|---|---|
| portrait | 46 | 75 |
| side-mounted (scaled 368/448) | 85 | 115 |

Portrait ships **±60** — already 1.30× the ostrich's clean limit, deliberately, on §10.4at's
argument that "a cropped toe at the peak of a gag reads as energy" on a 29 mm screen. Carrying
exactly that generosity across gives 85 × 60/46 = **110**, which is also what the limit is for:
it doubles as the *gain* (`tilt * max / LEAN_FULL`), so the same tilt now buys nearly double the
travel. Both constants moved to `face.h` so the host harness can pin them against the drawn
geometry — a lean limit that is only a firmware `#define` is a number nothing checks.

##### What the tests had to stop asserting

The first version of the lean test demanded the figure be wholly on screen at full lean, and
**portrait failed it** — the shipped ±60 has cropped the ostrich's tail since it landed. An
invariant the shipped product violates is not an invariant; it is a bug report about the test.
The property that survives is proportion: measure the room each fit has, require the
side-mounted limit to be as generous as portrait's and no more.

Three new tests, each confirmed to fail against the old code before being trusted:

- `test_a_tap_lands_where_the_pixel_it_touched_came_from` — for every pixel of glass inside the
  square, the frame pixel `panel_to_frame` names must be the one the rotated blit actually put
  there. Not "there is a mapping": the exact inverse of that one function.
- `test_the_zones_follow_the_scaled_figure` — a point and the figure move together, so a zone
  must not change when both go through the fit.
- `test_the_lean_limits_are_the_room_that_exists` — the proportion above, measured in-test.

And a defect in the harness itself: `firmware/host/Makefile` did not list headers as
prerequisites, so moving a constant into `face.h` produced a **green run against a stale
binary**. A suite that can report OK without having compiled the change is worse than no suite.

#### 10.4bx Two red lights, and the case had been eating one of them (0.2.64, 2026-09-22)

The owner, correcting me again and correctly: *"There is a small red light on the bottom left
and a larger red light on the top right. The larger red light only shows while I'm starting
recording. The bottom left one is always there regardless. It might be hidden from the
curvature while vertical?"*

Two lights. The large top-right one is `draw_listening`, as they said in §10.4bw. The small
always-on bottom-left one is the **caption's microphone-open indicator** — `caption.c`, drawn
whenever `c->live`, which is whenever the wake-word recogniser has the microphone open, which
is always. It is the one mark on this panel that is a promise to a room rather than a
decoration: the ICO Children's Code requires it while the microphone is open.

**And their hypothesis is right, measured against this repo's own number.** §10.4c, from the
first photograph of the hardware: *"the enclosure hides its corners — anything drawn there is
invisible to whoever is holding it."* `frontend/src/pet/scale.ts` acted on it the same day
(`CASE_CORNER_FRACTION = 0.13`, so 48 px of a 368 px width) and has clipped the PWA preview to
the case shape ever since. **The firmware never did.** The pip sat at x=10 on the caption row,
about 50 px from the bottom-left corner's centre of curvature against a radius of 48 — just
outside. So the compliance indicator has been drawn correctly and hidden by the enclosure for
the whole life of the device, and it took a quarter turn to reveal it, because a rotation maps
a corner of the SQUARE onto the middle of an EDGE of the glass.

The version label had the same fault more mildly: at (8, 6) its glyph box started ~58 px from
the same centre, so the leading `v` was chewed on every panel. Readable enough that nobody
filed it, which is exactly how it survived.

##### The thing worth keeping

**A screenshot could never have shown this.** The framebuffer was always correct. Every test
this firmware has, every host render, every reasoning-about-the-code pass — all of them
operate on a 368x448 rectangle that does not exist. The defect lives entirely in the gap
between what is drawn and what can be seen, and the only instrument that could detect it was a
person holding the object.

So the geometry moves into the firmware as `FACE_CASE_CORNER_R` and `face_inside_case()`, and
the host suite gains the question the framebuffer cannot answer: of the pixels this draws, is
every one of them somewhere a person can actually see? `test_the_microphone_light_is_inside_
the_case` asserts it for the indicator alone — the ticker deliberately scrolls in past the
corner, which is what a ticker does; a compliance light is not allowed to. Confirmed to fail
at the old x=10 before being trusted.

Both constants are hand-synced with `CASE_CORNER_FRACTION`; they live in different languages
and neither can import the other. That is a real seam and it is written down here because it
is the kind that drifts.

##### And the routing grammar the owner decided in the same message

*"I want the commands to be 'send xyz' or 'send to dad xyz'. If we didn't say 'to dad', default
the voice message to the other robot. Voice commands not starting with 'send' should go to the
LLM."*

One reserved word rather than a vocabulary of names, the common case (twin to twin) as the
no-argument default, and everything else falling through to the conversation path that already
exists. It is a better rule than the three-equal-recipients sketch it replaces, and it is
recorded in `../proposed/PANEL_CONVERSATION_PLAN.md` § "The grammar, decided" along with the
three things it still needs — a per-panel default recipient, a recipient table that holds
people as well as devices, and the prefix being stripped on the BOX, which is the only end
that has a transcript. Unbuildable until press-and-hold is confirmed on hardware; that
dependency has not moved.

#### 10.4by The black screen after every update was the reboot, not the rail (0.2.65, 2026-09-22)

The owner, with a dark panel minutes after the 0.2.64 update landed: *"Black screen right now —
do you want me to restart it, or are there logs you should pull? It seems after every
over-the-air update the screen is black these times."*

**Solved, by the instrument built for it, and the answer was not the one everything pointed
at.**

##### What the recording said

`pmu.h` exists for exactly this moment: the dark state cannot be observed live (QSPI reads
return zeros, and opening the USB console resets the S3 before anything can be seen), so the
AXP2101 and the TCA9554 are sampled every ten seconds into RTC memory, which survives
`esp_restart()` and is cleared only by pulling the plug. The standing hypothesis it was built
to test is in `display.c`'s own comment: *"it still blanks -> nothing the controller is told
matters, which points at the OLED rail and the AXP2101 this firmware has never spoken to."*

So the first thing to say was **do not power-cycle it** — that is the one action that erases
the evidence. The owner rebooted with the maintenance gesture instead, which is
`esp_restart()` and preserves the ring, and the boot telemetry carried eight samples from the
dark period:

```
20 15 4a 0f ff 01 cf ff ff     x8, dark
20 15 4a 0f ff 01 cf ff ff     the working panel, previous boot
```

**Byte-identical.** Status, chip id `0x4a`, and the three rail-enable registers `0x80/0x90/0x91`
= `0f/ff/01` — the OLED rail was powered the entire time the screen was black. The hypothesis
that had led since §10.4x is dead, killed by a measurement rather than by an argument.

Three more facts arrived with it: the panel **beeped on touch** while dark (firmware and touch
alive), `blit_ok` climbed with `blit_fail` at **zero** (transfers accepted), and the gesture
reboot **brought the screen straight back** (no power cycle needed, which is what everyone had
been doing).

##### The cause, which was in the diff of who calls `esp_restart()`

Frames going out, rails up, controller dark. That leaves the CO5300's own state — and the
difference between the reboot that fixes it and the reboot that causes it is *which task
restarts the chip*.

- **The gesture reboot** restarts from **inside the render loop**, at a point the code already
  chose deliberately (§10.4: *"the frame carrying a full-width cue has to reach the glass
  first"*). Nothing is in flight on the QSPI bus.
- **The OTA reboot** called `esp_restart()` from `app_main`'s loop, **asynchronously, while the
  render task was mid-blit.**

So the OTA cut a pixel transfer in half and the controller kept the half it got. It comes back
still waiting for the rest of a memory-write, and the next boot's init sequence is swallowed as
pixel data — including the driver's software reset, which is why that never rescued it:
`reset_gpio_num` is `GPIO_NUM_NC` on this board, so `panel_co5300_reset()` falls back to
sending `0x01`, and `0x01` is eaten as a parameter like everything else. The renderer then
queues perfect frames into a controller that is not listening, which is precisely the telemetry
signature: `blit_ok` rising, `blit_fail` zero, screen black.

A power cycle fixed it because it takes the controller's state with it. That is why the fault
looked like a rail problem for days: **the only known cure was removing power**, and that is
also what a rail fault would have needed.

##### The fix

`display_request_restart()`. The OTA sets a flag and the render loop restarts at the same point
the gesture always has — after the frame, nothing in flight. `ota_apply` then waits one second
and restarts from its own task anyway if the renderer never parks: the image is already
installed and marked bootable by then, so a dead render task must not be able to strand the
panel on the old one. Rebooting late beats not rebooting.

##### What is and is not verified

The mechanism is not a theory about what might be happening — the two reboot paths are a
controlled experiment that has now run many times, one leaving the panel dark on every update
and the other recovering it on every attempt, differing only in this. But **neither `display.c`
nor `ota.c` is in the host harness** (both are full of ESP headers), so there is no test here;
the build is the only static check.

The real verification is the next update, and it is self-announcing: if the panel comes up lit
without anyone touching it, this was it. If it comes up dark, the parking is not sufficient and
the next suspect is resynchronising the controller at init — sending enough `0x00` NOPs to
flush a half-consumed command before the reset, since a controller mid-parameter cannot be
talked to any other way.

Worth recording separately: **this cost a manual power cycle on every single deploy**, on a
device whose owner has no terminal and whose two units are going into children's bedrooms.
`CLAUDE.md` #10 calls that a gap to design out rather than a step to document, and it had been
quietly accepted as the cost of updating for long enough to be described as *"these times"*.

#### 10.4bz The microphone dot is gone, and the argument for it was overstated (0.2.66, 2026-09-22)

The owner, once 0.2.64 made it visible: *"The little LED on the bottom left turns on — I think
when the local model starts listening to audio, and it gains diameter as the mic volume
increases. I'd prefer just to remove that altogether. The red dot on the top right that says
listening I want to keep when I press it, but the other one on the bottom left is unneeded."*

Their reading of it is exactly right: `caption.c` eased a value toward 1.0 while the VAD heard
speech and 0.25 while the microphone was merely open, and the dot's radius scaled with it. So
it grew when the room got louder.

Removed, along with `caption_t`'s `live` and `lit`, the `live`/`speech` arguments to
`caption_tick`, the `pip` argument to `caption_draw`, and `MIC_COLOUR`. A feature deleted by
commenting out its draw call leaves dead state behind that the next person has to reason about.

##### The correction that matters more than the deletion

§10.4bx justified this dot as a compliance requirement — *"the one mark on this panel that is a
promise to a room rather than a decoration: the ICO Children's Code requires it while the
microphone is open"* — and `caption.h` said the same. **That was overstated, and it is worth
saying plainly because it was said in this plan, in a header, and in a test name.**

The Code's explicit *"obvious sign to children when it is active"* wording belongs to its
GEOLOCATION standard. Its connected-toys standard asks that a device include effective tools
for conformance, not that it carry a specific light. And the Code governs information society
services offered to the public — not a self-hosted panel a parent runs in their own house for
their own children, where the data controller and the parent are the same person.

The honest version: **it was a good idea argued as a legal one.** Dressing a design preference
in a regulation is how a preference becomes unarguable, and the owner should not have had to
argue with a citation to remove a dot from their own toy.

##### What is true, and now unmarked

The microphone IS always open — the wake-word recogniser needs it to be — and after this there
is no always-on sign of that on the glass. What remains is `draw_listening`, which marks the
press-and-hold recordings, and those are the ones that **leave the panel** for whisper and the
LLM. Continuous wake-word audio never leaves the device; it is matched on-chip against a fixed
command list and discarded. So the indicator that survived is the one covering the traffic that
reaches the network, which is the more defensible half of the pair if only one is kept.

##### The tests moved rather than went

Two asserted the dot. `test_caption_indicator_tracks_the_microphone` ("an open microphone is
always indicated", "muted is a promise") was the right assertion for a feature that no longer
exists, and is replaced by `test_the_ticker_draws_nothing_of_its_own` — with no phrase to show,
the row is empty and the pet underneath it untouched, which is what a deletion should leave
behind and is exactly where a stray pixel would survive.

`test_the_microphone_light_is_inside_the_case` from §10.4bx becomes
`test_the_case_geometry_is_the_case`. The dot is gone but the enclosure is not, and
`face_inside_case` still keeps the version label out of the corners. Pinning the predicate
directly also closes a hole in the old test: one that returned true everywhere would have
passed it just as happily, and would have been the more dangerous bug.

#### 10.4ca The loop closed, and the panel threw the answer away by 777 ms (0.2.67, 2026-09-22)

Press-and-hold ran end to end from the room for the first time. **It works.**

```
heard:  "Hello, one, two, three..."
reply:  "Hi there! Did you just count to three? What fun thing shall we do now?"

heard:  "Aardvarks love to eat mushrooms and bananas. One, two, three."
reply:  "Aardvarks are funny! Do you like mushrooms or bananas more?"
```

Real speech from a bedroom, transcribed correctly — including a sentence chosen to be hard —
answered on persona with a question back, synthesised, and returned. Every piece of the design
in `../proposed/PANEL_CONVERSATION_PLAN.md` holds.

##### The numbers nobody had

| | turn 1 (cold) | turn 2 (warm) |
|---|---|---|
| STT — whisper large-v3-turbo | 10,715 ms | 10,668 ms |
| LLM — gpt-oss-120b, `pet.turn` | **48,798 ms** | **1,540 ms** |
| TTS — Kokoro | 2,428 ms | 568 ms |
| **total** | **61,942 ms** | **12,777 ms** |

**The 48.8 s was not inference.** The adapter's own record of that same call says
`input_tokens: 191, output_tokens: 40, elapsed_ms: 1441, output_tokens_per_s: 27.8` — 1.4
seconds of generation inside a 48.8-second wait. The 01:06 update had explicitly evicted the
model (`[unload] released gpt-oss-120b`) and this was the first `pet.turn` since, so ~47 s went
on bringing a 120B model back up. The warm turn confirms it: same model, same prompt,
**1,540 ms**.

Two things that settle open questions in the plan:

- **The 191-token prompt is right.** §10.4bo's worry that a panel turn might drag the full agent
  context in was unfounded — `pet.turn` routes to `PANEL_CONVERSATION_PROMPT` and nothing else.
- **Whisper is flat, and it is now the whole problem.** 10,715 ms and 10,668 ms on utterances of
  very different length, because it pads every clip to 30 s regardless. That is **83% of a warm
  turn**, and no other component comes close. `base.en` stops being a nice-to-have and becomes
  the critical path.

##### And the panel discarded it

The warm turn returned **200 OK with 118 KB of speech at 12,777 ms**. `TALK_TIMEOUT_MS` was
**12,000**. So at 12.0 s the renderer called `talk_clear()`, went to `TALK_FAILED`, and drew the
failure face; at 12.8 s the reply arrived into a state machine that had already binned it — the
`TALK_NET_SPOKE` branch requires `s_talk == TALK_THINKING`, which it no longer was.

**Everything worked and the answer was thrown away three quarters of a second before it
landed.** 12,000 was a guess made before any turn had ever been measured, and it happened to
sit just below the real number.

Raised to **25 s**, with `talk.c`'s HTTP timeout to **30 s** — that ordering is the point rather
than the values: the socket must never die while the face is still willing to wait. The gap
between them is why the capture buffer is guarded on `TALK_NET_BUSY` (§10.4bn), and that guard
is what keeps the widened window safe.

It is a **safety net, not a target.** A longer net costs nothing when turns are fast; it only
matters when they are slow, and a slow turn currently produces *nothing*, which is strictly
worse than a late answer. Nobody should read 25 s as the intended experience — the intended
experience needs whisper to stop taking eleven seconds, and no timeout value improves that.

##### What is still true

A 12.8 s wait for a four-year-old is too long, and this release does not fix that; it stops the
system discarding work it has already done. The next measurement worth taking is `base.en`
against the same two utterances, since the whole latency argument now rests on a single flat
number with an obvious lever on it.

#### 10.4cb A reply is an utterance, and everything else on the panel is an interjection (0.2.68, 2026-09-22)

The owner, straight after the first real conversation: *"When the agent is talking we should
prohibit beeps from cutting it off, and we should also stop poke interactions making other
animations, because we should make an animation of the robot talking as it talks."*

Three requests that are one rule. A reply is the only sustained thing this panel ever says, and
a beep over it is a toy talking over a person. `speaking` (`audio_playing()`) is now decided
once a frame and every branch defers to it.

- **No beep, no colour change, no new action** while the reply plays. The flinch stays — a pet
  that ignores a finger entirely reads as frozen — but the stage is not taken over.
- **The mouth moves.** A `talk` channel on `face_state_t` rather than an `action_t`, because a
  reply can arrive mid-wave and a pet that stops waving to talk reads as two pets. The ostrich
  drops its lower mandible (the split is between the two wide beak segments and the two narrow
  ones, so the gape opens where a bird's does); the robot opens a mouth under its smile, so the
  smile stays the lip of it rather than being replaced by a hole.
- **A hold cannot start a recording while we speak**, and this one is measured rather than
  tidy. At 01:53:54 the owner held to ask a second question while the first reply was playing
  and the recording came back **empty** (`heard: ""`). `audio.c` deafens the microphone for six
  chunks whenever the speaker runs — the codec routes the DAC into the ADC, so without that the
  pet transcribes itself — which means a hold taken over our own voice can only ever capture
  silence. Refusing it costs nothing and stops a child being ignored by a toy that looked like
  it was listening.

##### The mouth is an honest fake, and the reason is worth keeping

Two frequencies, not one: 6.3 Hz for the syllable rate and 2.7 Hz for the phrase, so the product
never quite repeats. Same argument as the jittered blink — a mouth on a single sine is a
metronome and the regularity is what gives away a machine. It never fully shuts while talking,
because a beak closing between syllables reads as chewing.

It is **not driven by the actual audio**, and that is a limit rather than an oversight: the one
signal that could give a real envelope is the microphone, and `audio.c` deafens it whenever the
speaker runs precisely so the pet does not transcribe itself. An honest fake at the right rate
beats a real envelope the hardware refuses to supply.

##### And the red failure face the owner saw was already fixed

*"I got the little red even though it successfully sent a message to the server and then got a
response back — maybe we need to extend the time on that?"* That is §10.4ca: the 12 s timeout
against turns measuring 11,661 and 13,897 ms, straddling it. 0.2.67 raised it to 25 s and this
release carries that. No further change needed; the turns were measured, and 25 clears the
worst of them by 11 seconds.

##### What the test pins

`test_the_mouth_moves_only_while_talking` asserts two things, and the second is the one that
catches a careless channel: the mouth must visibly change the drawn face (a `talk` field nothing
reads would pass anything weaker), the change must land on the head rather than anywhere else,
and at `talk = 0` the frame must be **byte-identical** to what it was before this channel
existed — because a channel that leaks at rest changes every frame the pet has ever drawn.
Both forms, since a change reaching only one of them is half a feature. Confirmed to fail with
the mouth stubbed out.

#### 10.4cc He walks when he moves (0.2.69, 2026-09-22)

The owner: *"the robot should kind of shuffle his legs back and forth as tilt causes him to
move left and right."*

The lean has slid the figure downhill since §10.4 and the legs have never once acknowledged it
— the pet travels the width of the panel like a chess piece. So: a walk.

##### The phase advances with distance, not with a clock

This is the whole design and it is the part worth defending, because the obvious implementation
is a timed oscillation gated on "is he moving" and it is wrong in two ways a screenshot cannot
show. It keeps stepping for a frame or two after he stops, and it takes the **same number of
steps to cross the panel slowly as quickly** — which is exactly what a walk is not.

Driving the phase off pixels travelled makes the relationship the real one: a step per 20 px of
ground, so he takes more steps when he goes further and **none at all when he is still, with no
gate to get wrong**. Amplitude is separate and eased, and follows speed rather than distance —
a slow drift is a shuffle, a fast slide is a scramble — with a fast attack and a slow release so
a stride finishes instead of being cut off the instant the panel stops moving.

Applied on top of whatever the action posed, like the lean itself: a pet tilted mid-wave keeps
waving and moves its feet. The robot swings its legs; the ostrich also lifts, because
§10.4's note stands — a bird drawn head-on has no depth to step into, so its stride reads as a
lift, and the lift has to be in phase with the swing or the raised foot is the one taking the
weight.

##### In `rig.c`, which is the only reason it has a test

`display.c` cannot be linked by the host harness, so a walk written there would have shipped on
an argument. `rig_walk()` is pure C, and the suite checks the property a timer would fail:

```
twenty pixels covered in 20 frames of 1 px  ->  phase 3.1416
twenty pixels covered in  4 frames of 5 px  ->  phase 3.1416
```

Plus the legs alternating rather than swinging together (what a careless `+=` on both would
give), a stationary pet leaving the action's pose **exactly** untouched, the faster crossing
taking the wider stride, and the phase wrapping — because a panel left tilting accumulates
travel forever, and a float big enough that one frame's addition rounds away would stop the legs
dead. Confirmed to fail against a clock-driven phase before being trusted.

#### 10.4cd "Hey fish" — a name, and finding the end of a sentence (0.2.70, 2026-09-22)

The owner: *"both of these panels will have a wake word that will allow the same interaction as
if I held the panel and it was listening ... but we just need a way for emptiness at the end to
stop it."* The twins named this one **fish**.

##### It needed no wake-word engine, and that was the surprise

WakeNet only recognises models Espressif has trained; "fish" is not one and cannot be added.
But §10.4ar's configuration already solved this by accident: **WakeNet is disabled** and
MultiNet runs continuously over a command list, registered from **plain English text** at
runtime (`esp_mn_commands_add(i, "change into merc")`). So the panel's name is one more row in
`vocab.c`, and `esp_mn_commands_update()` already supports re-registering it live.

The endpointing signal was likewise already there and unused. `speech.c` has set
`s_hearing = res->vad_state == VAD_SPEECH` since bring-up, to gate MultiNet and drive the
indicator. The front end has been deciding "is someone talking" every frame and nobody had
asked it the question the owner just asked.

##### Two words, and the first choice did not work

The twins' first name was **robot**, and it is unusable for a reason nothing to do with
preference: `vocab.h` rule 3 forbids a phrase being a prefix of another, and **"change into
robot"** and **"be a robot"** are already in the table. Bare "robot" breaks all three.

"hey fish" clears that, and the carrier word earns its place independently. Every other phrase
in the table costs an animation when it misfires. **This one opens the microphone, uploads six
seconds of a child's bedroom, calls a language model and makes the pet talk to an empty room.**
`speech.h`'s warning that "a one-word always-on vocabulary fires at the television" stops being
a style note at that price, and a carrier word is what every always-on device puts in front of
its name for exactly this reason.

##### Three ways out, and each is a different sentence to a four-year-old

A hold has a release. A name does not, so the end has to be found:

| | | |
|---|---|---|
| **hush** | 900 ms of VAD silence after speech | send it |
| **lead** | the name, then nothing for 3 s | **drop it silently** |
| **full** | `audio.c`'s six-second cap | send what we have |

The middle one is the important one. An accidental "hey fish" off the television must cost
**nothing** — no upload, no bubble, no reply to an empty room — and that branch is the only
thing standing between a false trigger and a conversation with the TV. 900 ms is the compromise
on the other end: long enough to survive the pause a four-year-old puts in the middle of a
sentence, short enough that the six-second cap does not eat the tail of a slow one.

It reaches **the same `TALK_LISTENING` state a hold does**, deliberately — the bubble, the
upload, the reply and the failure face are the press-and-hold machine, and the only difference
is how the turn ends. Refused while a turn is in flight or while the pet is speaking, for the
same reasons the hold is.

##### The test, and the gap

The suite pins that there is **exactly one** name (two would give the panel two names and the
twins no way to know which worked; zero would leave the hands-free path unreachable with
nothing to say so) and that the phrase carries a space — the carrier word, asserted rather than
remembered.

**The name is compiled in, and that is a gap.** §10.4ab's argument applies exactly: the owner
has no terminal, so a name only a rebuild can change is a name they cannot change — and the two
panels will want different ones. It belongs on `endpoint_settings` beside the other knobs, with
the panel re-registering on the settings fetch it already makes at boot.

**Unverified:** every timing here is reasoned, not measured. Whether 900 ms cuts a four-year-old
off mid-sentence, and how often "hey fish" fires at a television, are questions only the twins'
bedroom answers. The trigger logs, so false fires can be counted from telemetry rather than
guessed at.

#### 10.4ce The beep and the voice stop sharing a volume (0.2.71, 2026-09-22)

The owner: *"can we have the beep down to say volume 20, and the [speech] to volume 90?"*

Two changes, and only one of them is comfortable.

**The beep and the voice have shared a single codec output for the whole life of this
project.** So the acknowledgement tone has always been *exactly* as loud as the pet's speech —
and the beep is the part fired on every poke, the part with a hard transient in it, and the part
held closest to an ear. Splitting them costs nothing, because the tone is synthesised here:
scaling its amplitude by `BEEP_VOLUME / VOLUME` lets the voice get louder while the beep gets
quieter. Done as an amplitude ratio rather than a second codec call, because `esp_codec_dev` has
no locking and one task owns the codec — changing the output level around every beep is exactly
the cross-task poke that panicked a panel in §10.4al.

The reference point matters: 9000 was the peak when the codec sat at 70 and the beep shared the
voice's level. Without the ratio, raising the output to 90 would have made the beep **louder**
at the same moment the request was to make it quieter.

##### The uncomfortable half, recorded rather than quietly changed

`audio.c`'s header opens with *"VOLUME IS A SAFETY LIMIT HERE, not a preference"*, cites ASTM
F963 / EN 71-1 capping close-to-ear toys at **65 dB(A)**, and ends *"raise it only against a
measurement."* This raises it to 90, and **there is still no measurement.**

What there is instead is a parent who has listened to the thing in the room it lives in — which
is the only instrument this project has ever had for this number, and is why 70 was a guess too.
90 is also the vendor's own figure for this hardware. So the change is reasonable and it is
still a limit crossed on judgement rather than on data.

It is written into the header rather than left in a commit message so the next person to read
that paragraph knows the limit was crossed deliberately and by whom. **A sound level meter
would settle it in a minute**, and a 29 mm speaker a four-year-old holds to their ear is the
right place to spend that minute. Until then the beep going *down* is the part of this change
that reduces exposure, and it lands on the sound that fires most often.

#### 10.4cf He has to come home (0.2.72, 2026-09-22)

The owner, on 0.2.69's walk: *"the foot movement while tilting is now great. However, if we're
static and not moving very fast or kind of just sitting there, the legs need to be back in the
neutral position."*

Correct, and it was **three faults stacked**, only one of which is in the walk itself.

##### 1. A crawl is not a walk

`s_lean` is smoothed and integer, so it converges on its target by ever-smaller steps, and the
accelerometer keeps nudging it by a pixel at rest. Without a floor that trickle is
indistinguishable from a very slow walk: the phase creeps, the amplitude never quite reaches
zero, and the legs sit forever at some arbitrary point in a stride. A panel on a shelf was
walking imperceptibly.

`RIG_WALK_DEADBAND_PX` (1 px/frame, so 25 px/s — eight seconds to cross the panel) stops
**both** the phase and the amplitude. It also gets the physics right for free: the tail of every
real movement falls under the floor as the lean converges, so he decelerates into a stop rather
than being cut off at one.

##### 2. The settle was drawn at five frames a second

The real one, and it is not in `rig.c` at all. `dirty` is set while the lean is *changing* — so
the moment the lean reached its target the render loop dropped to the 200 ms idle floor **while
the stride was still settling**. Fourteen frames of decay at 5 fps is nearly three seconds of a
pet standing on a shelf with one leg out. That is what was being seen.

`if (walk.amp > 0.0f) dirty = true;` — the same rule the flinch and the blink already follow:
animating means every poll is a frame. Worth recording because the symptom was entirely in the
legs and the cause was entirely in the frame pacing, and no amount of tuning in `rig_walk` would
have fixed it.

##### 3. And the release was too slow anyway

0.08 per frame is about two seconds even at full rate. 0.25 settles in fourteen frames — still
visibly a settle rather than a snap, and done in half a second. The attack stays at 0.35: legs
pick up quickly and put themselves down deliberately.

The phase now homes to zero once the amplitude does. The legs are already neutral at zero
amplitude — the pose is amplitude times the swing — but leaving the phase where it stopped means
the next step begins mid-stride from a standing start.

##### Both new checks fail independently against the old code

Removing the deadband fails *"a crawl leaves the legs standing"*; restoring the 0.08 release
fails *"about half a second after stopping he is standing again"*. Verified separately, because
two fixes landing together are exactly where one of them turns out to do nothing.

#### 10.4cg A band you have to mean to cross, and a button nobody has read (0.2.73, 2026-09-22)

##### The orientation flipped on noise, and the hysteresis that "existed" was not where it looked

The owner: *"the tilt going to 90 and causing an orientation change shouldn't happen right at
45. We should have like an extra 20 you should have to go in order to cause the orientation
change, and then another 20 back past that 45 to go back the other way."*

`FLIP_THRESHOLD` has been in this file since §10.4 and its comment says *"hysteresis at about
half a gravity"* — which is true and is about a different thing. It gates **how much gravity is
in the XY plane before the reading is trusted at all**, so a panel lying flat does not flip on
noise. **Which quarter** a trusted reading meant came from `|ax| > |ay|`, and that comparison
turns over at exactly 45 degrees with no hysteresis whatsoever. Hold a panel at 45 and the two
axes are equal, so a millivolt of accelerometer noise picks the orientation — several times a
second, which is what the owner was watching.

Worth recording as a shape rather than a bug: there **was** a constant named for hysteresis, it
**was** doing its job, and the thing it guarded was not the thing that needed guarding. A
comment that is accurate about the wrong quantity is harder to see past than no comment.

The quarter now comes from the **angle** of gravity in the plane, and the current quarter keeps
it until the angle is more than 45 + `ORIENT_HYST_DEG` from that quarter's own centre. Turning
from upright toward landscape the flip lands at 65 degrees; coming back, 65 degrees from the
landscape centre is 25 degrees from upright. A 40 degree band either side of the boundary,
which is what was asked for.

**In `orient.c`, so it can be tested.** `display.c` cannot be linked by the host harness, and an
orientation rule that is only reasoned about is exactly how the first flip shipped backwards in
0.2.19. The suite holds it at 45 from both sides and shows it does not move, jitters it 400
times across the boundary and counts **zero** flips, and checks each quarter is reachable from
the one opposite — a panel set down and picked up the other way should land where it *is*,
rather than stepping round through a neighbour.

##### And the buttons: measured, not assumed

The owner: *"there are two switches on this board, one labeled power, one labeled boot. Can we
utilize those to basically turn off the microphone with one of them?"*

A mute is worth having and this firmware has never read either button, so the first question is
which of them it **can** read. BOOT is GPIO0 on every ESP32-S3 board there is — but "every board
there is" is not this board, and this plan's history is full of pin maps that were obvious and
wrong. `audio.c`'s header opens with two pins named from opposite ends of the same link, where
guessing gives silence *and* a dead microphone with no error from either.

So 0.2.73 **counts edges on GPIO0 and reports the count in telemetry**. Press it a few times and
the panel answers the question instead of me. Configured as an input with its pull-up and never
driven, because this pin is the boot strap and driving it is a way to make a panel unflashable.

PWR is almost certainly not a GPIO at all — it goes to the AXP2101, whose latched PWRON bits sit
in registers `pmu.c` does not sample. That is the next probe if BOOT comes back alive and one
button turns out not to be enough.

**The design question a mute raises, before it is built:** a firmware mute is a promise, not a
wire — the microphone keeps running and the code chooses to discard. And §10.4bz removed the
always-on "microphone is open" dot at the owner's request, which was right because it was always
on. A **muted** badge is the opposite case: it appears only in the rare state, it is the only
way to tell a muted panel from a deaf one, and without it the first support question is "is it
broken or did someone press the button?"

#### 10.4ch A beak you can see from across a room, and a conversation that continues itself (0.2.74, 2026-09-22)

##### The mouth was sized against a host render, not against a bedroom

The owner, watching the ostrich talk: *"the mouth movement is definitely not big enough or
obvious enough that his mouth is moving for talking."*

§10.4cb dropped the lower mandible 11 px and split the beak so only the narrow tip moved. That
is legible in a 368×448 host render at desk distance and invisible on a 29 mm screen across a
room — which is the only distance that matters.

The hinge moves up (the widest segment alone is the upper mandible; the other three swing as one
jaw), the drop is **progressive** so the jaw pivots rather than sliding down in one piece, and
the gape goes to 30 px — most of the beak's own height. The robot's mouth gets the same
treatment for the same reason.

**And the test was complicit.** `test_the_mouth_moves_only_while_talking` asserted the open
mouth changed more than **80 px** and passed cheerfully on a mouth nobody could see. A threshold
below the smallest thing a person would accept is not testing what it is named for. Measured
after widening — 721 px on the ostrich, 838 on the robot — the floor is now **500**, which
catches a regression toward subtle while leaving room to restyle.

##### And then it listens again, without being asked

The owner: *"after the text-to-speech comes back and finishes talking, we should just turn the
microphone on and start recording again, and if I start talking within 2 seconds, just
automatically record all that until I stopped talking again and send that as the next turn. That
way I can have fluid conversations."*

Which is the difference between a toy you operate and one you talk to. The machinery already
existed: this is §10.4cd's hands-free listen with a different trigger and a shorter lead, so the
whole feature is *notice the reply finished, and open the window the name opens*.

Fired on the **edge** where the speaker falls silent, not on a timer — a long reply must not
have the microphone opened underneath it. `audio.c`'s six-chunk deafness after the speaker runs
conveniently keeps the tail of our own voice out of the front of the next recording.

**No beep on this one.** A tone after every reply is the toy interrupting the conversation it
just started; the red indicator is the affordance, and by the second turn a child knows what it
means.

##### The cap, because this is a loop with a loudspeaker in it

Every reply reopens the microphone. A television talking in the room can therefore hold a
conversation with the panel **indefinitely**, each turn costing a whisper pass, a model call and
a synthesised voice — and §10.4cd's wake phrase means it need not even be started by a person.

Six consecutive follow-ups is far more than a four-year-old's exchange and bounds the runaway.
After that it wants a deliberate start — the name or a finger — either of which resets the
count. The refusal logs, so a panel that keeps hitting the cap says so rather than quietly
talking to a television all afternoon.

#### 10.4ci The smile is the lower lip, not a second mouth (0.2.75, 2026-09-22)

The owner: *"when changing to the robot, when it speaks the happy face doesn't go away while
the mouth appears, which looks really weird."*

Correct, and §10.4cb earned it. The open mouth was a rounded **box** drawn under the smile arc,
on the stated theory that "the smile stays the lip of it rather than being replaced by a hole."
It does not. A curved smile with a rectangle below it reads as **two mouths**, because that is
what it is — and the comment asserting otherwise is why it shipped twice.

The opening is now bounded **by the arc**: for every column across the smile, fill from the
arc's own y upward by the gape, tapered to nothing at the corners so the hole is a lens rather
than a band. That is the cartoon convention — the mouth opens out of the smile line and the
smile becomes the bottom of it — and it collapses exactly to the untouched arc as the gape goes
to zero, with no pop and no second shape.

##### The corners are the property, and the threshold is measured

A real mouth closes where the lips meet. A box is full height right out to its edge. So the
test walks the per-column height of the opening and compares the centre against nine tenths of
the way out, **on both sides** — a taper on one side is a shape that slid rather than a mouth
that opened.

Nine tenths is measured, not picked: the lens runs 23 px at the centre and 8 px at 90%, while a
rounded box of the same width is still near full height there because its corner radius only
bites in the last fifth. Confirmed by putting the old `fill_round_rect` back and watching the
check fail.

This is the second time in two releases that the mouth's test was weaker than the owner's eye
— §10.4ch raised a visibility floor that had passed on a mouth nobody could see, and this adds
the shape check that would have caught a box pretending to be a mouth. Both were found by
someone looking at the object, which remains the instrument this project cannot replace.

#### 10.4cj The corner says whose pet it is (0.2.76, 2026-09-22)

The owner: *"top left where we have the version number, if I touch that it should change
between the version number and the panel name. Default to only showing the panel name."*

The right default, and it was not the right default for most of this project's life. The
version earned that corner while every other message in this session was *"which build is it
on"* — and it stops earning it the moment there are **two panels in two bedrooms** and the
question becomes whose. A four-year-old cannot read `0.2.76` and can read their pet's name.

The version is one touch away rather than gone, because it is still the first thing anyone
debugging this asks for and telemetry is not in the room with you.

##### The name comes from the wake phrase, not from a second constant

`vocab_name()` returns the word after the carrier in the `VOCAB_LISTEN` phrase — "hey fish"
gives "fish". One place to change it when the phrase becomes a per-panel setting (§10.4cd's
open gap); a second constant holding the name would be a second thing to forget, and the two
would drift the first time only one of them was edited.

The host suite pins the derivation: the word after the carrier, plain lowercase, drawable in a
font that has uppercase, digits and one lowercase `v` and nothing else. Confirmed to fail
against a broken split.

##### The label is its own button

Checked **before** the zones, so a corner of the glass that says something cannot also be a
poke — a tap that both flipped the label and made the pet sneeze would read as two things
happening at once. It costs nothing: the label sits above the pet's head where `face_zone`
returns nothing anyway.

In frame coordinates, so it follows the quarter turn like everything else a finger touches
(§10.4bw), and padded 14 px beyond the glyphs in every direction — the text is 14 px tall and
a four-year-old's fingertip is not, so the target is the corner rather than the letters.

#### 10.4ck The encoder window is a form field, and the config was a file nobody could write

Two halves of the whisper problem §10.4ca measured: a warm transcription of real speech takes
**9.55 s**, which is 83% of a conversation turn, and it is flat because the encoder always
processes a fixed 30 seconds of mel while the panel records at most six.

##### The lever turned out to be a per-request field, not a launch flag

Reading the **pinned** whisper.cpp v1.7.4 source rather than assuming: `server.cpp:418` takes
`audio_ctx` as a multipart form field, and it flows into `wparams.audio_ctx`. So the encoder
window can be sized **per clip**, from the backend, with no host file involved — which was the
entire blocker on the largest lever.

`WhisperCppClient.transcribe()` takes optional `audio_ctx` and `language`. **Defaulting to
`None` is the load-bearing part**: this client is shared with the agent's transcribe tool,
which is handed recordings of arbitrary length, and a window sized for a toy would truncate
them. The test asserts the fields are *absent* when nobody asks, which matters as much as their
presence.

The panel's turn computes it from the clip — ~50 encoder frames per second of audio, half as
much again for margin, floor 256 — so a four-second utterance asks for 300 against 1500. It is
logged beside `stt_ms`, so the effect is measured rather than asserted.

Reading the source also caught what would have been an outage: **an unknown argument makes
whisper-server `exit(0)`**. A guessed flag would have taken STT down on a box nobody can shell
into. `-ng/--no-gpu`, `-l/--language` and `-ac/--audio-ctx` were all confirmed present before
anything was written.

And `--language en` came back *out* of the launch config: the server is shared, so forcing
English at launch would impose it on the agent's tool. Per request, from the caller that knows.

##### The config was a file only a sudo host script could write

`whisper-models/llama-swap.yaml` was generated once by `scripts/whisper-setup.sh` and **never
regenerated by an update** — so a flag added to the repo reached a live box only if somebody
re-ran the provisioner by hand, and the owner of this deployment has no terminal to do it from.
A PWA toggle could not have existed.

`deploy/whisper-config.sh` now generates it and **both** callers use it — the provisioner and
every update. Deliberately the lesson written at the top of `update-inner.sh` ("It is one file
because it was two, and they drifted"), applied before it happens rather than after.

Safe to call unconditionally, which is what lets an update do it: no models directory or no
model in it writes nothing and exits 0. The filename is **discovered** from disk, so a box on
`base.en` is not repointed at a model it lacks. Written atomically, because llama-swap watches
that path and a half-written config is no transcription at all.

##### Vulkan, built but not used

`Dockerfile.tts-stt` said a CPU build "keeps it self-contained" and that large-v3-turbo "is
quick on the Strix Halo CPU". The measurement says otherwise. It now builds with
`GGML_VULKAN=ON` — the base image is already the Vulkan llama-swap image.

**Building with it is not using it.** `--no-gpu` is emitted unless `WHISPER_GPU` is exactly
`true`, defaulted false, because this iGPU is also serving `gpt-oss-120b` and an eviction costs
~47 s to reload. The owner asked for a switch for precisely this reason and the switch is the
point.

**And the build falls back.** A Vulkan configure that fails — missing headers, no `glslc`, an
SDK the base image moved — would otherwise abort the image, and this image is *also* Kokoro:
the box would lose read-aloud as well as transcription to a speed optimisation. A failed Vulkan
build reconfigures for CPU and says so, which is where `--no-gpu` would have left it anyway.

##### Not yet done

The PWA toggle, and the measurement. `audio_ctx` ships in this change and is the one that can
be measured immediately — the next thing said to the panel will report its own window beside
its own `stt_ms`.

#### 10.4cl The mid-transfer theory was wrong, and the workaround is a second boot (0.2.77, 2026-09-22)

§10.4by root-caused the black-screen-after-every-update to the OTA's `esp_restart()` cutting a
QSPI pixel transfer in half, and 0.2.65 fixed it by parking the renderer first. **That theory is
now falsified.** The panel updated to 0.2.76 using 0.2.69's parked restart — from a
power-cycled, known-good controller, which was the clean test — and came up black exactly as
before. Rails identical through the dark period for the third time.

The story fitted every observation available and was still wrong. Worth recording as that
rather than quietly replaced: "frames accepted, rails up, a restart cures it" is consistent with
several causes, and the one that got written down was the one that had a mechanism attached.

##### What the evidence still says

A **second boot of the same image** cures it, every time, with nothing reinstalled. That has
held across many updates and is the only reliable cure short of pulling the plug.

And the remaining suspect has a name now. §10.4x recorded that the vendor BSP leaves both
`BSP_LCD_RST` and `BSP_LCD_BACKLIGHT` at `GPIO_NUM_NC`, and concluded *"so neither is a line we
are failing to drive."* **That conclusion assumed a GPIO.** The TCA9554 expander at `0x20` is
where such a line would be if it is not one — and `pmu.c` reads its configuration register as
`0xff`, every pin an input, nothing ever driven. So the CO5300 has very likely **never had a
hardware reset in this project's life**, only the driver's SWRESET fallback, which a controller
mid-command eats.

That is a hypothesis, not a finding. Confirming it needs the schematic, and **probing it blind
is the one thing not worth doing** — an unknown expander output could be a power rail or the
touch controller, and that is a guess that damages hardware rather than wasting a cycle.

##### The workaround, labelled as one

After an update, boot once more. Exactly once: the flag lives in RTC memory, is cleared before
the restart so nothing can loop, and a power cycle randomises the word into something the magic
rejects.

**Placed after `ota_confirm_health`, and that ordering is the entire safety argument.** A new
image boots in `PENDING_VERIFY`; restarting before it is marked good makes the bootloader **roll
back to the previous firmware**. A double reboot dropped in carelessly would have quietly undone
every update it was meant to rescue — which would have looked, from the outside, exactly like an
update that failed to take.

It also sits after `report`, so the boot that went dark still gets its telemetry out before the
restart. Two seconds is worth less than the evidence.

#### 10.4cm "Stop" (0.2.78, 2026-09-22)

The owner, one release after the follow-up loop landed: *"we also need a keyword added, 'stop',
that will stop the conversation. Now that it auto continues for six turns, it wants to keep
going even if I say stop."*

Which is the obvious consequence nobody thought of: §10.4ch built a conversation that continues
itself and gave it no way out. Saying "stop" was a sentence like any other — transcribed, sent,
answered, and followed by the microphone opening again. The only exit was to stop talking and
wait the loop out, which is a strange thing to ask of a four-year-old who has just asked it to
stop.

##### On the panel, because that is where the loop lives

MultiNet resolves it on-chip in under half a second, so it takes effect **before a recording is
even uploaded** — and it works when the box is slow or unreachable, which is precisely when a
child would most want to give up. Routing it through the transcript instead would have made the
way out depend on the thing being escaped.

Three states to leave, because "stop" has to mean stop wherever it is said: a recording in
progress is **dropped rather than sent**, a reply already in flight is **abandoned rather than
spoken**, and the follow-up window is closed so the microphone does not reopen. The turn counter
goes to its cap rather than to a separate flag — the next deliberate start (the name, or a
finger) resets it, which is the rule that already governs the loop.

##### The single-word list grows, and this one is easy to argue

`vocab.h` requires two words and keeps the exceptions as a **named list** so each new one has to
be argued rather than slipped in. "stop" is the easiest argument that list has had: every other
entry costs something when the television says it — a wiggle, or six seconds of a bedroom
uploaded to a model. **A false "stop" ends a conversation that was not happening.**

##### The limit, stated

**"Stop" cannot be heard while the pet is speaking.** `audio.c` deafens the microphone whenever
the speaker runs — the codec routes the DAC into the ADC, so without it the pet transcribes
itself. So an interruption mid-reply still waits for the reply to finish, and then the word
lands in the follow-up window where it does work. Barge-in is a different feature and needs the
codec problem solved first, which §"Step 8" of the conversation plan already had as an open
question.

#### 10.4co Confirmed on the hardware (0.2.79, 2026-09-22)

```
04:20:31  v0.2.79  uptime 9s  sw(3)   first boot after the OTA — dark
04:20:45  v0.2.79  uptime 9s  sw(3)   restarted itself — lit
```

Two boots, fourteen seconds apart, nobody touching the panel. The owner: *"display worked."*

**The recurring cost is gone**: every deploy in this session until now ended with a manual
reboot, on a device whose owner has no terminal and whose two units are going into children's
bedrooms. That is what `CLAUDE.md` #10 calls a gap to design out, and it is now designed out —
by a workaround rather than a fix, which is worth keeping straight.

**What is still unknown is the cause.** Three theories were tested and two were wrong: the
DMA/underflow family (§10.4bf), the mid-transfer restart (§10.4by, falsified in 0.2.76), and
the OLED rail (§10.4x, killed by the PMU ring reading byte-identical through three separate
dark periods). The remaining suspect is the CO5300's reset line: the vendor BSP leaves
`BSP_LCD_RST` at `GPIO_NUM_NC`, and the TCA9554 expander — whose configuration register reads
`0xff`, every pin an input, nothing ever driven — is where that line would be if it is not a
GPIO. Confirming it needs the Waveshare schematic. Probing it blind does not: an unknown
expander output could be a power rail or the touch controller, and that is a guess that damages
hardware rather than costing a cycle.

So the second boot stays until someone reads that schematic, and this section is the note that
says why it is there — because a workaround nobody remembers is a workaround nobody removes.

#### 10.4cn RTC memory does not survive an OTA (0.2.79, 2026-09-22)

0.2.77's second-boot workaround **never fired**, and the telemetry says exactly why. Two boots,
two hours apart, same panel:

| | `crash_phase` | PMU ring |
|---|---|---|
| after a **gesture** reboot | 9 | 8 samples |
| after an **OTA** reboot | **−1** | **empty** |

**`RTC_NOINIT` survives `esp_restart()` of the same image and not an OTA.** Those variables are
placed by the linker; a new image is a new link, its RTC data lands at different addresses, and
the incoming firmware reads the outgoing one's bytes. The restage flag was written by the old
image and looked for by the new one at an address that meant something else.

Nothing broke — the magic rejected the garbage, exactly as `pmu.h` designed it to after §10.4am
— so the restart simply did not happen, silently. Which is the worst way for a workaround to
fail: it looked like the fault it was meant to fix.

##### The signal was already there, and it needs no storage

`ESP_OTA_IMG_PENDING_VERIFY` is true on exactly the first boot of a newly installed image and no
other. It asks the system the question instead of asking memory nobody controls, and a layout
change cannot confuse it. `ota_confirm_health` was already reading it three lines away.

Read **before** that call, because marking the image good clears it, and held in an ordinary
local — the value only has to survive a few hundred milliseconds, not a reboot.

##### What this says about everything else in RTC

`pmu.c`'s ring and `display.c`'s crash breadcrumb have the same limitation, and it is now
measured rather than assumed: **both are blind across an update.** That is tolerable — each
exists to explain a crash or a dark screen on a panel that restarted itself, which is the same
image — but it is worth knowing that the one boot they cannot describe is the boot after an
update, which is precisely the boot that has been going dark.

##### And the BOOT button is real

`boot_btn: 1` in the same report. The owner pressed it, GPIO0 counted it. The pin map guess was
right and is now a measurement, so a mute switch has somewhere to live (§10.4cg). PWR remains
unprobed and still most likely belongs to the AXP2101 rather than to a GPIO.

#### 10.4cp Faster turns, a conversation it remembers, and the data that only existed on a cable (0.2.80–0.2.83, 2026-09-22)

Six complaints in one message, and they turned out to share two causes.

**The turn took 9.5 s and should have taken 1.3 s.** `audio_ctx` was being sized from a
constant floor of 256 frames regardless of how long the clip actually was, so a two-second
utterance paid for a much longer window. Sized from the clip instead
(`max(160, min(1500, seconds * 50 * 1.5))`) and the round trip fell to **1.3 s** — measured,
not estimated.

**The babies were being cut off**, both ends. Two separate faults wearing one symptom:

- *At the end of their turn*: the stop margins were too tight for a four-year-old who pauses
  mid-sentence. `_TRIM_LEAD_MS` 200 / `_TRIM_TAIL_MS` 400.
- *During the pet's reply*: `PANEL_AUDIO_MAX` was a single ceiling serving both directions,
  and a long reply hit it — **261,290 bytes against a 192,000 limit**, truncated silently.
  `PANEL_REPLY_MAX` split it out and a `converse_reply_truncated` warning now says so when it
  does happen rather than leaving a sentence to stop mid-word.

**The silence at both ends is now trimmed** (`_trim_to_speech`): a 20 ms frame peak series, a
gate at 3× the 10th-percentile frame or 300 absolute, and the margins above. One sampled clip
was **six seconds of mostly room**. The first cut of this also carried a `ref//4` cap and a
`_TRIM_MIN_MS` floor; probing showed neither ever bound — the margins alone already give
620 ms against a 600 ms floor — so both came out. A guard that cannot fire is a guard nobody
can reason about.

**The stop word is "fish stop"**, matching the panel's name. (Changed again in §10.4cr.)

##### The replies were incoherent, and history was why

Half the sampled replies did not follow from what the child said — *"it had jam on it"* was
answered with *"was it on your tummy or a yummy cookie?"*. Every turn was being sent with no
history at all, so the model was answering a fragment. `_panel_memory` now carries **5 turns
with a 240 s TTL**, keyed per panel. Babbles are deliberately NOT remembered: a recogniser
false-positive should not become context the next three turns are conditioned on.
`PANEL_CONVERSATION_PROMPT` rewritten to name subjects a small child has (what they ate, what
they did today, their toys) and to stop offering games it cannot play.

##### `burp` was never a missing sound; it was a phrase that never fired

Reported as *"the code word is wrong"*. It was not refused — `vocab=46/0` proves every phrase
registers — and the earlier claim here that it was a refusal was **wrong**. `speech_heard()`
was added to carry the decoded phrase, its probability and whether it fired, and the answer
came back on the wire: **"spin" fired at p=13/100.** This is a confidence problem, and setting
an honest floor needs more samples than one session gave. Still open. What did ship: six more
ways to ask (`can you burp`, `can you fart`, `do a big jump`, `have a snack`, `give it a kick`,
`do a spin`), because a phrase a child actually says is worth more than a threshold.

###### The samples arrived, and "a confidence problem" was the wrong frame

**2026-09-24, firmware 0.3.00, one 15-minute window on Lydian's panel** — the capture that entry
was waiting for. Every phrase that fired: `eat` 13, `eat` 16, `stop stop` 23, `fart` 13,
`laugh` 19, `spin` 26. Alongside them, **81 `?` entries** — decodes that timed out without
resolving to any command.

The owner, testing by hand in the same window: `spin` works, `dance` does not. `do a dance`
works. `hey fish` works. `send a message` and `send dad a message` do not.

**The failing phrases never appear in the ring at all, at any probability.** They are in the `?`
bucket. That is not a phrase scoring under a bar — `speech.c` says outright *"There is no
confidence floor here"*, so anything that decodes fires. `dance` is not losing a comparison; it
is not being decoded.

Which inverts the fix this entry proposed. **A floor set from these numbers would take the
feature backwards**: the working phrases sit at 13-26, so any threshold high enough to suppress
a false trigger would also silence `eat`, `fart` and `spin`, while doing nothing whatever for
`dance`. The floor is not the missing piece; it is a trap that looks like the missing piece,
and it stayed open for a version because nobody had the sample set to see that.

What actually separates the two groups is whether MultiNet's grapheme-to-phoneme pass resolves
the phrase at all, and on this evidence a short word is the weakest case — `dance` fails where
`do a dance` succeeds, same action, same model, same session. **So the fix is phrasing, not
thresholds**, which is what "six more ways to ask" was already groping toward. See
`vocab.c` on the send phrases, reworded to `tell dad` / `tell sister` on the same evidence.

##### What was only visible on a cable, and now is not

The instruction was to review every avenue of data reachable only over USB and put it in
telemetry. `TelemetryIn` gained `vocab_ok`, `vocab_bad`, `vocab_refused`, `heard`,
`int_largest`, `levels`, `blit_fail_total`, `blit_recov`, `meter_fail`, `wifi_reason`,
`wifi_drops`, `ota_err`, `ota_tries`, `restart_why` — and **`tap`**, which the panel had been
sending all along and the box had been silently dropping. That one was found by a test
asserting that every key the firmware writes is a key the model accepts, which is the kind of
gap no amount of reading either side finds.

Three real bugs fell out of writing it:

- **`ota_report` returned `ESP_OK` for any HTTP status.** A 422 counted as a successful report
  and **cleared the PMU crash ring** — the panel discarding the evidence for a crash the box
  had just rejected.
- **`main.c`'s telemetry body could ship without its closing brace**, because the `}` was
  written inside the last optional block. Unconditional now, and the buffer grew to 1536.
- **`esp_mn_commands_add`'s return was discarded**, so a refused phrase looked identical to an
  accepted one. Checked, counted, and the first six refusals are reported verbatim.

##### Residency: the pin, and what a pin cannot do

The owner: *"I would rather keep OSS 120 loaded all the time and then the Qwen models be able
to hotswap first."* `keep_loaded` now ranks pinned models last among eviction candidates in
both planners, and `qwen3.5-4b`'s `runtime_overhead_gb` came down from 9.5 to **3.5** on a
measurement — 7.85 GiB of whole-box GTT against a 15.0 GiB declaration.

**The pin did not stop the eviction, and the earlier claim here that it would was wrong.** A
pin orders the victims of an eviction *decision*; the 120B is being killed by a llama-swap
**config reload**, which makes no decision and consults no ranking — `--watch-config` sees the
file change and `old.Shutdown()` takes down every running server. `llama_swap_config.write()`
already compares content and short-circuits, so something is genuinely rendering differently,
and `render()` is not pure: it globs the filesystem through `resolve_weight`. Which entry
differs is **still unknown**. `llm_settings.gateway_config_changed` now logs exactly what
changed when it re-stamps; reproducing needs the 4B to cold-load while the 120B is resident.

##### And the double-boot workaround does not work

§10.4co said it was *"confirmed on hardware"*. `restart_why: 'ota-park'` in today's telemetry
proves the restart **fires** — and the screen was still black afterwards. So the correction
stands: it fires, it does not fix it, and the five-tap gesture reboot remains the only thing
that brings a panel back after an update. Root cause still open; the remaining suspect is the
CO5300 reset line behind the TCA9554 expander, which needs the Waveshare schematic rather
than blind probing next to a power rail.

#### 10.4cq Twenty-six sounds, because the pet already varied and only the noise stayed put (0.2.84, 2026-09-22)

The owner, twice: *"Sound effects are too repetitious. Same with the poke."* Then, precisely:
*"I want the poke of the screen to be kind of dependent. If you hit the head the robot should
kind of coo and be kind of nice... You can't all just be the same little coin sound effect.
List all of the actions. All of the things that can cause sound effects — make sure they're
all unique and appropriate."*

**The panel was already varied; the sound was the only part that was not.** A poke resolves
through `variants.c` to a weighted random action from the touched zone's pool — the head can
blush, giggle, nod, wiggle or fall asleep — and every one of those played the same 880 Hz
tone. So the fix is not to randomise a noise. It is to let the sound follow the **action**,
which was already the interesting thing: `audio_cue(cue_for_action(action))`. Poking the head
coos because the head's pool leans towards a blush. **No zone is special-cased**, so the sound
and the animation cannot drift apart, and they stay in step for free the next time the pools
are re-weighted. The same rule now covers a *spoken* action too, which retires the burp/fart
special case — asking for a thing sounds like the thing, for all nineteen rather than two.

`cue.c` is 26 cues over eight oscillator shapes: the nineteen actions in `rig.h`'s order, then
seven interface sounds (tap, label toggle, calibration tick, phrase understood, mic open,
stop, failed turn — the last of which was **silent** before this).

**Synthesised, not sampled**: twenty-six WAVs is a licence question, a download and most of a
megabyte of flash to answer what a table and eight shapes answer.

**Additive, not naive squares**: output is 16 kHz, so Nyquist is 8 kHz and a hard-edged 2 kHz
square folds its 10/14/18 kHz harmonics back to 6/2/2 kHz — on top of the real partials and
sliding the *wrong way* as a sweep moves. Summing sines never generates a partial above
Nyquist. Pulses use the **cosine** basis (peak ≈1.18, the Gibbs overshoot) rather than sine
(peak ≈2.7, which wastes 7 dB of headroom for the same waveform).

What the research established and the table encodes: SMB's coin is B5→E6, an ascending
perfect fourth, **83 ms into 799 ms** — the duration asymmetry does as much work as the
interval. A jump differs from a laser by *rate* (≈2.8 oct/s against 15–25), which is why
CUE_JUMP holds a flat plateau first. Roughness is a property of **register**, not interval:
partials buzz when they fall inside one critical band, ≈30 Hz apart near 300 Hz, where the
same interval two octaves up merely beats — so CUE_OOPS is a close pair down at 311 Hz, low
and falling and gently rough. *Try again*, not *told off*.

**Each cue also varies between plays.** `cue_render` takes a `variant`: pitch moves by up to
two semitones, length by 7%, and multi-note shapes rotate which interval they lean on.
Contour, register and shape never vary, because those *are* the meaning — a rising cue that
sometimes falls is not a variation. `audio_cue` counts the variant **per cue** rather than
drawing at random: what has to differ is two consecutive plays of the *same* sound, which is
what a child poking the same spot four times produces, and a counter guarantees that where a
draw only makes it likely.

##### Three things the host suite caught that listening would not have

`cue.c` is pure C with no ESP-IDF in it, specifically so it can be tested on a host — the
generator it replaces (`audio_rude`) could not be, and shipped with no test at all.

- **A cue rendered nothing.** The fart is 620 ms and stretches to 663; `CUE_MAX_SAMPLES` was
  9600. The length guard swallowed it *silently*, which is the worst shape for that failure.
- **22 KB of floats on an 8 KB stack.** Peak-normalisation is done by measurement rather than
  arithmetic (the naive bound is 2.7 for a square whose real peak is 1.18, and it *moves* as a
  sweep carries partials through the taper). Buffering the floats to find that peak would have
  overflowed the render task. The generator is pure for a given `(cue, variant)`, so it simply
  runs **twice** and stores nothing.
- **A DC offset of 13% of full scale on the eat.** A slowly-clocked LFSR is a sample-and-hold
  of a coin flip, and a 300 ms cue only gets ~500 clocks of a 32767-step register — the flips
  do not average out, so the noise rides a random offset. Measured across seeds: 200 clocks
  can land at **49%**. That parks the cone off-centre for the length of the sound and snaps it
  back at the end, which is the same click the envelope exists to prevent arriving by another
  route. Which seeds land badly is luck, so the fix had to be structural: one pole at 200 Hz
  inside the noise generator, well under the kilohertz it is clocked at and well over the
  fraction of a hertz the drift lives at. Eat 3356 → −31, kick 1173 → 7, sneeze 471 → −9,
  fart 409 → 3.

That last one is also why the click and centring tests now sweep **32 variants** rather than
checking the nominal one: the defect is redrawn per variant, so a single-variant test proves
nothing about the rest. It reproduces the failure when the DC blocker is removed.

##### Deleted rather than kept

`audio_beep` and `audio_rude` are gone. Nothing called the first once every site had a cue,
and it held a 2,880-byte tone buffer in the **internal** RAM `speech.c` is tight on. The
second hand-rolled its own sawtooth, envelope and clipping — the one generator in the firmware
that could not be built on a host, and the only one that stayed at a fixed level while the
rest were peak-normalised. CUE_FART and CUE_BURP are the same sounds through the tested path.

#### 10.4cr Five farts, and a way out a child would actually say (0.2.85, 2026-09-22)

Two asks, and the second one is a design point rather than a request for more content.

##### "stop stop"

The way out of a conversation has now been three things: bare `stop`, then "fish stop", now
**"stop stop"**. §10.4cm argued for the name version on symmetry — a conversation ends with
the pet's name the way it starts with one ("hey fish") — and that argument **was wrong about
the user**. A four-year-old who wants it to stop is not composing a phrase. Repetition is what
escalation sounds like at four; "stop stop" is already what they say, where "fish stop" is a
thing they have to remember to say. The phrase most likely to be uttered in the moment it is
needed wins, and it is not always the tidiest one.

It costs nothing that bare `stop` did not already cost less of — a television has to say it
twice in a row — and `vocab.h` rule 3 still holds.

The test that pinned this changed shape rather than its threshold, which is the honest move
when a premise goes away: "carries the panel's name" is gone, because it is no longer true.
What it pins now is the two properties that survive every rewording — the phrase is **more
than one word** (or it is always live and the television ends conversations), and **nothing in
the table shadows it**, because a stop phrase MultiNet will not resolve is a child shouting at
a toy that keeps talking, and that failure is silent.

##### Five farts

The owner: *"fart should have five different kinds of farts, different tones, length,
squeakiness, etc. The kids really love the farts."*

**The variant machinery every other cue uses is not enough here, and that is the finding.**
§10.4cq's `variant` moves pitch by up to two semitones and length by a tenth. For a giggle
that is plenty. For a fart it is nothing: **a fart transposed a semitone is the same fart.**
Variation that preserves identity is the right default — it is what stops a giggle becoming a
different sound — and it is exactly wrong for the one cue where the *identity* is supposed to
vary. So the fart gets five rows of its own rather than one row and a knob.

Four axes actually distinguish one from another, and every row moves all four:

| | length | starts | contour | texture | wet |
|---|---|---|---|---|---|
| **rumbler** | 700 ms | 92 Hz | falls | slow flutter | barely |
| **squeaker** | 220 ms | 340 Hz | **rises** | tight, buzzy | dry |
| **sputterer** | 420 ms | 150 Hz | falls | **breaks into bursts** | a little |
| **wet one** | 560 ms | 118 Hz | falls | unhurried | 0.75 |
| **pfft** | 170 ms | 210 Hz | falls hard | shallow | 0.55 |

Lengths span **a factor of four** and registers nearly **two octaves**, against the two
semitones the knob offered. The squeaker is the only one that rises, which is why `fall` is
allowed to be negative: a tight opening pinches *higher* as it closes.

**The sputterer is a clamp, not a number.** Its tremolo depth of 0.92 would drive the envelope
negative, and a negative envelope **inverts the waveform rather than interrupting it** — a
phase flip, which is audible as harshness and not as a gap. Clamping at zero turns the same
number into real silence between bursts, which is what sputtering actually is. (A corner in an
envelope does not click; a jump would.)

`variant` still applies on top, so it is 5 characters × 8 transpositions × 4 stretches, and 5
and 8 being coprime means the counter in `audio_cue` walks all forty rather than cycling five.

##### Measuring "squeakiness" honestly

The first attempt to test this measured **the decay envelope and called it texture**: counting
frames quiet relative to the cue's *global* peak marks the end of every cue, so all five
scored 0.35–0.68 and the test proved nothing. Measured against each frame's own ±30 ms
neighbourhood instead, the number says the one thing sputtering is — the flow stopping and
restarting *while the sound is still going* — and the separation is unambiguous: **0.53 for
the sputterer against ≤0.05 for the rest** (worst case across all forty variants: 0.485
against 0.150).

Register is measured as a low-frequency energy share rather than a pitch, for the same reason
§10.4cq switched estimators: three of the five carry noise, and a crossing-rate estimate on a
noisy signal reports the **noise bandwidth**, not the fundamental. The first probe cheerfully
reported the wet one, whose fundamental is 118 Hz falling to 72, as rising through 843→982 Hz.

The test checks each axis **separately** — length, register, texture, then pairwise
distinctness — because a single "are the waveforms different" check passes on five rows that
differ in one number, which is precisely the failure being guarded. Probed against two
degenerate implementations: five rows differing only in pitch (fails on length), and the
tremolo clamp removed (fails on sputter).

One sound was tuned by this rather than by ear, and the direction was already right: the
pfft's flutter depth came down from 0.45 to 0.25. At 48 Hz a deep flutter reads as buzz rather
than rhythm anyway, and nothing escaping that fast has time to flutter.

`CUE_MAX_SAMPLES` is now 12800 (800 ms) for the 700 ms rumbler, which stretches to 749 — with
deliberate room above it, because §10.4cq's silent-swallow is what a tight ceiling here looks
like.

#### 10.4cs The reset line that was there all along (0.2.86, 2026-09-22)

**The panel has never had a hardware reset, and that is the black screen.**

`display.c` brings the CO5300 up with `.reset_gpio_num = GPIO_NUM_NC` and a comment asserting
*"the panel has no reset line brought out; the init sequence does the work."* That comment is
wrong, and everything downstream of it followed.

With no reset pin, the controller only ever gets a **software** reset — a command, down the
same QSPI bus as everything else. From a cold boot that is fine: the CO5300 resets itself when
the rails come up. It cannot work in the one case that matters. `esp_restart()` leaves the
controller **powered and holding its state**, so a CO5300 stopped mid memory-write is still
waiting for pixels — and it swallows the next boot's entire init sequence as picture data,
**including the software reset**. Nothing sent over that bus can reach it.

That is the whole shape of the fault, and it finally explains the parts that never fitted:

- **Why the screen is dark while the firmware is provably fine.** §10.4am measured rails up,
  firmware alive and beeping, `blit_ok` climbing with `blit_fail` at zero — frames going out
  to a controller that was not listening. Exactly what a wedged memory-write looks like.
- **Why the reboot gesture works and the OTA's own restart does not.** §10.4cl blamed leaving
  mid-transfer and 0.2.77 parked the renderer to fix it; telemetry confirms the park FIRES
  (`restart_why: "ota-park"`) and the screen is still black, which §10.4cp recorded as a
  correction without explaining it. Now it explains itself: how the panel leaves was never the
  variable. The gesture works because it is a *later* reboot, by which time the controller has
  been fed enough bytes to finish the write it was stuck in.
- **Why waiting helps.** The owner: *"I don't want to wait 15 minutes. Can I just do the
  gesture update"* — waiting was already known to work, and nobody had a reason for it.

##### The line exists, and `pmu.c` has said so since it was written

*"The BSP brings the panel's reset and enable lines out here, which is why a chip nothing has
ever written to can still be holding the screen off."* The suspicion was recorded; what was
missing was **which pins**, and probing blind next to a power rail and a touch controller was
never worth the risk.

Waveshare's own V2 sample code answers it. Every display example for this board does the same
thing before touching the touch controller or the display:

```cpp
expander.pinMode(0/1/2, OUTPUT);
expander.digitalWrite(0/1/2, LOW);
delay(20);
expander.digitalWrite(0/1/2, HIGH);
```

A 20 ms active-low pulse on **TCA9554 pins 0, 1 and 2**. Verified identical across
`04_GFX_FT3168_Image`, `02_Drawing_board` and `13_LVGL_Widgets` in the `arduino-v2` tree —
this board's revision, not V1's.

##### Confirmed against the box's own telemetry

The PMU ring that survived the 0.2.85 OTA reads, on all eight samples:

```
20 15 4a 0f ff 01 | cf ff ff
                    in out cfg
```

**`cfg = 0xff` — all eight expander pins are INPUTS.** Nothing is driven, exactly as `pmu.h`
says. External pull-ups hold the three reset lines deasserted, which is precisely why a cold
boot works and why no reboot has ever been able to assert them.

(Those samples are a *healthy-panel* baseline — they cover the two minutes before the OTA
reboot. A dark-period capture would still be worth having, and the gesture is how to get one.)

##### What shipped

`pmu_reset_panel()` pulses pins 0–2 low for 20 ms and releases them, then settles 120 ms
before the init sequence goes out. It is called from `display_start()` immediately after
`pmu_start()` — which already owned the expander handle — and before the SPI bus comes up.

Three things it deliberately does:

- **Read-modify-write, three bits only.** The other five are not ours: two read low on this
  board and the vendor drives a sixth for the SD card. A blanket write is how a diagnostic
  becomes an outage.
- **Deassert before switching to outputs**, so becoming an output cannot glitch the lines low.
- **Degrade, don't refuse.** If the expander does not answer, the bring-up is exactly what it
  has always been — a panel that boots the old way beats a panel that will not boot.

`panel_reset` is now in telemetry (and in `TelemetryIn`, because a key the panel sends and the
box drops is a bug this plan has already had once). A dark panel reporting `panel_reset: true`
and one reporting `false` are different bugs, and until now every boot was silently the
second.

**Not yet confirmed on hardware.** This is an I2C write sequence that cannot be exercised on
the host. The prediction is specific and cheap to falsify: a boot of 0.2.86 should report
`panel_reset: true` with the expander's config byte reading `0xf8` instead of `0xff`, and a
panel that goes dark after an update anyway would then be a DIFFERENT fault from this one.

**And the claim this entry was first written with is already too strong.** It said 0.2.86 fixes
"the very boot that has gone dark after every single update". The 0.2.85 OTA did not go dark:
the owner found the screen on, and telemetry agrees — 2757 s of continuous uptime, no gesture
reboot, `restart_why: "ota-park"`. So **the black screen is intermittent, not deterministic**,
which matters twice over. It weakens the evidence that the missing hardware reset is the cause
(a wedged controller would not unwedge itself between updates), and it means a single good boot
of 0.2.86 proves nothing on its own — only a run of updates that all come back lit would. The
missing reset is a real gap worth closing either way; whether it is THE gap is still open.

#### 10.4ct A finger that cancels, and a name that stops being the question (0.2.87, 2026-09-22)

Two asks from the owner, and the second one is not where it looks like it is.

##### A touch cancels a listen

*"When it's listening, if I touch the screen it should stop and discard."*

A hands-free listen had no way out for someone who was not going to say a phrase. It runs until
the room goes quiet, so an accidental wake — or a child who changes their mind — was committed
to a turn they did not want. A finger is the one input that is always available and never
ambiguous.

It does exactly what `"stop stop"` does: the recording closed and dropped, the follow-up window
shut so the microphone does not reopen, and the turn counter parked at its cap until a
deliberate start resets it. Two ways to say stop that behaved differently would be a worse toy
than one that only had a word.

Two deliberate limits. **Only the voice-started listen** — a held turn ends on the RELEASE of
the same finger that started it, so treating that touch as an abort would make press-and-hold
impossible to complete. And **the tap is consumed**: no colour change, no action, no poke. A
touch that both cancelled the question and made the pet fart reads as two things happening and
the child cannot tell which one they asked for. The flinch stays, because something has to
acknowledge the finger.

##### The name stops being the first word of the question

*"If I say hey fish and then proceed with asking it something, it shouldn't be transcribed hey
fish at the beginning."*

**The obvious fix is on the panel, and it is the wrong one.** The recogniser fires on the
phrase, so opening the microphone after it leaves the name already past — which is exactly what
happens on a cold start, and is not the path this shows up on.

The path is the follow-up. After a reply the panel reopens the microphone by itself for
`FOLLOW_LEAD_MS` (2 s), and a child who starts their next sentence with the pet's name is
recorded saying **all** of it. The recogniser does fire, but `VOCAB_LISTEN` is refused because a
turn is already live, so nothing trims anything and the whole utterance goes up. **The name
arrives inside the audio**, so it has to come off the text.

Stripped rather than left for the model to ignore, because it is not inert: it is the subject of
the first sentence the model sees, and *"hey fish, what do dogs eat"* gets answers about fish.

Three properties, each with a test: it comes off **only as a prefix** (a name mid-sentence is the
child talking about the pet, and deleting it would change what they said); it does not eat a word
that merely starts with the name (`"hey fisherman"` survives — the trailing `\b` is the whole
reason); and an utterance that was ONLY the name becomes empty, which the caller already treats
as "say that again" rather than an error. That is the right answer to an accidental wake and a
better one than a reply about fish.

The spelling variants are Whisper's, not ours — it has no idea this is a name and spells it by
sound. And the coupling is pinned: a test reads `VOCAB_LISTEN`'s phrase out of `vocab.c` and
asserts the box strips it, so **renaming the pet fails loudly here** instead of silently
restoring the symptom.

##### A note on the suite, and on measuring before concluding

28 backend tests failed on the first run of this change and none of them were related to it —
`RecorderRefused: there is only 465 MB left on the box`. The sdr recorder asserts a free-space
floor, and repeated full-suite runs had left 8.1 GB of pytest temp directories behind, taking
the session's writable allowance to 99%. Cleared, the suite is 6555 green.

Worth recording because the first two diagnoses were both wrong: "pre-existing" (reached by
running the failing file alone, which passes, rather than the suite that fails) and then "mine"
(reached from a clean-tree comparison that happened to run when there was more disk). Neither
hypothesis was tested against the actual error text, which named the cause in one line.

#### 10.4cu Voice post on the panel (0.2.88, 2026-09-23)

W3 of `JPANEL_PLAN.md`: the panel can now send a message and play one. Built and byte-compared
in CI; **not yet run on a unit**, so everything below is what it does, not what it has been
seen doing.

##### Two phrases, and the margin between them is one letter

`send a message` goes to the other panel; `send dad a message` goes to the PWA. Neither is a
prefix of the other — they diverge at `a` against `d` — and that is the whole margin rule 3
leaves. The shape is what protects it: every send phrase names its recipient BEFORE the noun,
so a third one (`send ellie a message`) is safe for the same reason, where the natural-sounding
`send a message to ellie` would make the short form its prefix and take the feature down. A host
test pins the shape rather than the two strings.

##### Recording is a state, not a flag on listening

`TALK_RECORDING` sits beside `TALK_LISTENING` because three things differ — where the audio
goes, what is drawn, and what ends it — and a flag would have every branch of the machine
asking "but which kind". The one that forgot would upload a child's message to the pet, which
would then answer it out loud.

Everything tuned for a four-year-old is shared: the same 1.8 s hush, the same cap, the same
finger-cancels rule. One number differs, `RECORD_LEAD_MS` at 4 s against the name's 3 s —
asking the pet something is a sentence already formed, telling your sister something is one
being composed out loud by a child who has just watched a blue dot appear.

**Blue, not red**, which is the owner's requirement rather than a palette choice: red means *the
robot is listening to you*, and talking to your sister must not look like that.

##### The pop-up, and the button that would have stopped working

A waiting message draws a box over the pet, tappable anywhere inside, naming who it is from. It
does not auto-play — a message that started talking on its own would be the panel making noise
in a bedroom at a moment nobody chose.

The first cut cleared the rectangle on the tap and thought that was enough. It was not: the
waiting count does not drop until the box hands the message over, so the very next frame drew
the pop-up again, over a fetch that `jpanel_play_next` now refuses. A child would have been
pressing a button that had stopped working. The draw is gated on `JPANEL_BUSY` as well.

##### The truncation that would have cut Dad in half

`audio_play` capped everything at ten seconds — the REPLY ceiling — and did it silently. A
message from Dad is typed text through a voice, and `SendText` allows 600 characters, so every
long one would have stopped mid-word with nothing in any log to say it had. This is the same bug
as the 261 KB reply in §10.4, arriving by another route: two different things sharing one
number because nobody had separated them. The buffer is now sized by the LONGEST audio any
caller can hand over (20 s, matching `MAX_MESSAGE_MS` on the box) and says so when it still has
to cut. The reply keeps its own cap in `talk.c`.

##### Two new cues, because reusing one was the old bug

"Sent" first used `CUE_TOGGLE` — the sound the name/version label makes. That is the twenty-six-
events-one-blip problem coming back one event at a time, so there are now `CUE_SENT` (three
notes climbing away, unresolved on the octave, because a sent message is not finished — someone
else has it now) and `CUE_MESSAGE` (a soft falling third on a triangle with a slow attack). The
arrival cue is the only one in the set that fires with nobody having touched or said anything;
it goes off in a bedroom, so it announces rather than demands. Both clear the suite's
"no two cues are the same sound" check on their own.

##### The 404 that would have looked like silence

**Every jpanel call the firmware made was to a route that does not exist**, and it took asking
the live box to find out. `JPANEL_PLAN.md` §3b put the panel's routes on the device surface at
`/api/endpoint/jpanel/*`, beside `/endpoint/converse`, and said in as many words that it was
the source of truth. W2 had mounted one `/jpanel` router carrying the panel's four routes and
the owner's four together, so the live paths are `/api/jpanel/*`. W3's firmware was written
from the plan.

It built, linked, passed 4,045,509 host checks and produced a byte-compared image. **Nothing
could have caught it**: the URL is assembled by `snprintf` inside a file that cannot be built
on a host, and on a panel the symptom is not an error — `GET /waiting` returning 404 is
indistinguishable from "nobody sent me anything", so the whole feature would have shipped
looking merely quiet. A child would have said *"send dad a message"*, watched the blue dot,
spoken, and been told it was sent.

Found by probing the box directly: `/api/jpanel/waiting` answered **401**,
`/api/endpoint/jpanel/waiting` answered **404**.

Fixed toward what is deployed rather than the reverse — the backend is live, the PWA already
calls it, and renaming production routes to match a document costs a redeploy for nothing. Auth
is per-route (`PanelDep` against `OwnerDep`), so sharing a prefix with the owner's routes is not
a hole; it does mean the device surface is no longer one prefix, which is worth weighing if a
wall ever gates by path.

**Pinned from the end that can run.** `test_the_panel_facing_routes_are_where_the_firmware_looks`
reads the format string out of `firmware/main/jpanel.c` and asserts the four route paths on the
router, so either side moving fails loudly. Verified by reintroducing the bug and watching it
fail. The general lesson is the one §10.4 keeps relearning: a coupling that no single package
can see needs a test that reads both ends.

##### And then panel-to-panel turned out never to have worked at all

Two more, found the same way — by asking the live box instead of reading code. Neither is in
the firmware; both are in W2, and both would have made a child's message to her sister vanish.

**RLS meant a panel could not see any panel but itself.** `principals_select` opens for the
owner, for `auth_ctx()` in ('login','bootstrap'), and for a principal reading its own row.
`send` resolved "the other panel" inside a session scoped to the *asking panel*, so the roster
held exactly one entry — itself — and `others` was always empty. **Every sibling message
answered 409, on any box, from the first commit.** The same cause had two quieter symptoms: the
pop-up never learned who a message was from, and `GET /next`'s `X-Jpanel-From` always said "the
other one".

Fixed by reading the roster under the narrow `login` context in a session of its own, not by
widening the policy — and that ordering is the point. A panel must not be able to enumerate
principals; it says "the other panel" and the box decides who that is. Widening
`principals_select` to let device keys see each other would hand a device on a bedroom wall the
whole principal table, `key_hash` column included, to answer a question it should never have
been asking.

**And every `/flash` mints a key that nothing retires.** The box carried **thirteen** unrevoked
principals labelled `panel Elora` — one physical panel, re-flashed — plus two unnamed. Fifteen
candidates where the route needs exactly one, so even with the policy fixed it would have stayed
at 409. The roster keeps one row per NAME now, newest `created_at` winning, which is right
rather than tidy: `/flash` rewrites the unit's NVS, so the newest key for a name is the one that
panel is running and every older one is dead by construction.

`send` also excludes by NAME rather than by id, because a panel still on a superseded key is not
in the roster under its own id — `pid != principal.id` would leave its own name in the list and
post the message back to the unit the child spoke into.

Both are pinned by integration tests against real Postgres, each verified by reverting the fix
and watching the test fail. The residual risk is written into `JPANEL_PLAN.md` §5: a flash that
minted a key and then failed leaves a newest key no unit holds, and nothing records which key is
live, because `last_used_at` is never written and RLS forbids any panel- or login-context write
to `principals`. That is the panel roster §5 keeps asking for.

##### What is known to be missing

- **A panel cannot learn the other panel's name.** The blue indicator says `TO DAD`, or
  `MESSAGE` for the twin. There is no route that answers "what is the other unit called", and
  inventing a word for a child's sibling would be worse than saying MESSAGE. Same missing
  mechanism as `JPANEL_PLAN.md` §5's panel roster.
- **The cap is ten seconds, not the plan's twenty.** W3 reuses `audio.c`'s single capture
  buffer, as the plan told it to; twenty would mean a second 320 KB buffer or doubling a
  conversational cap whisper is already sized against. The `full` branch logs when a child
  actually hits it.
- **`X-Jpanel-Id` cost a trap worth naming.** `esp_http_client_get_header()` reads the REQUEST
  headers — it returns what the panel sent, not what the box answered — and compiles, runs and
  hands back NULL forever. Response headers reach a caller through the event handler alone.
  Losing that id means a message that plays every time the panel asks.


#### 10.4cv Dad in the pet's voice, a silent pop-up, and two seconds of waiting (0.2.89, 2026-09-23)

Three things the owner found by using it, an hour after the first messages went through.

##### Dad arrived in the robot's voice

`DAD_VOICE` was `"am_michael"`. `_resolve_kokoro_voice` in `deploy/tts-stt/tts_server.py`
returns the DEFAULT for any id that does not start with `kokoro-`, and the default is
`CURATED_KOKORO_VOICES[0]` — `af_heart`, **the pet's own voice**. So every message the owner
sent arrived in exactly the voice a separate voice exists to avoid, because a message from Dad
in the robot's voice teaches a four-year-old that the robot and their father are the same thing.

**It was silent by construction and that part is not a bug.** The fallback is right on the
engine's side — a stale id from an old client should render rather than error — so the box
logged a successful render, the panel played perfectly good speech, and nothing anywhere said
the voice had been swapped. It took the owner hearing it.

Now `"kokoro-am_michael"`, and pinned: a test reads `KOKORO_ID_PREFIX` and
`CURATED_KOKORO_VOICES` out of the service and asserts the id carries the prefix, is in the
roster, is male, and is **not** the default. Verified by restoring the old string and watching
it fail. The two live in different packages with different test runners and nothing else
connected them.

##### The pop-up made no sound, and the message took two seconds to start

The gap was the whole round trip — a TLS handshake, a blob read, a rate conversion and up to
640 KB down the wire — and it ran AFTER the tap, because the tap is what started it.

**Nothing required that order.** `GET /next` deliberately does not mark a message played, which
is what makes a message survive a power cut mid-playback; the same property makes it free to
collect one EARLY. So the ~30 s poll that discovers a message now also fetches it, and the tap
is a memcpy into the speaker's buffer. The slow path survives for the case the fast one cannot
cover — a pop-up tapped before the poll had collected the audio — and now plays on arrival
rather than making the child tap twice.

Two details the prefetch forced, both of which would have been bugs:

- **A background fetch reports no state.** It runs on the jpanel task's own clock, so writing
  `s_state` would overwrite a `JPANEL_SENT` the renderer had not shown yet — and the child
  would lose the sound that told them their own message went. Only a fetch a finger asked for
  has an outcome worth reporting.
- **The tap sound is conditional, and the obvious version is wrong.** `audio_play` refuses
  while anything else is sounding, so an unconditional acknowledgement beep would be the thing
  that swallowed the message now that playback usually starts on the same frame. The message IS
  the acknowledgement when it plays at once; the cue fires only when it does not — which is
  exactly the tap that felt unanswered.

##### And the pop-up stands down after fifteen seconds

The owner: *"the notification on the panel is very large when it shows which is fine, but if
it's not acknowledged within say 15 seconds, it should kind of be a smaller one up on the top
left."*

A box over the pet's face is right for the first fifteen seconds — it has to interrupt, the
reader is four and is not auditing the screen for changes. It is wrong for the next hour: a
message nobody has come to yet should not hold a child's toy hostage. So it becomes a badge
top-left, still carrying the sender's name and still the same generous tap target — shrinking
the box must not shrink what a four-year-old has to hit.

Two details that are easy to get wrong:

- **The clock starts when the WAIT does, not when the count last moved.** A second message
  arriving while the first is unheard must not restore the big box: the child has already been
  interrupted once and has chosen not to come. It restarts only from nothing-waiting.
- **The shrink is a frame nobody else asks for.** The count has not changed, no finger has
  landed, and the pet may be perfectly still — so without an explicit repaint the big box would
  sit there until the next blink happened to redraw it.

The AGAIN button owns the same corner for its five seconds and wins there: it is transient and
answers a question the child is asking right now, where the badge answers one they have already
declined.

#### 10.4cw The message that came back every thirty seconds (0.2.90, 2026-09-23)

The prefetch in 0.2.89 was correct about the network and wrong about who sets a flag. The owner,
within the hour: *"it seems that right now it just keeps repeating the test message every 30
seconds or every poll."*

**The acknowledgement was armed by WATCHING FOR A STATE that another task clears first.**
`jpanel_play_next` runs on the RENDER task and sets `JPANEL_PLAYING`; the jpanel task polled for
that state every 250 ms to know it owed the box a `POST /played`. In between, the renderer's own
`switch (jpanel_state())` clears it — **in the same frame as the tap**, because `speaking` is
sampled at the top of the frame and the tap that starts the audio happens later in that frame.
So on exactly the frame a child presses the pop-up, `speaking` still reads false, the renderer
decides the message has finished, and the state is back to IDLE before the jpanel task has
looked once.

The box therefore never learned the message was played. It stayed unplayed, the next poll
fetched it again, the pop-up returned, and the panel repeated the same message in a child's
bedroom every thirty seconds indefinitely.

Two fixes, and the first is the general lesson:

- **A flag set where the work happens cannot be missed by a reader that runs later.** `s_owed`
  is now set synchronously at the moment `audio_play` succeeds, in both places audio can start.
  The polled version never had that property; the code it replaced did, because it armed inside
  the task's own command branch. Refactoring moved the work to another task and quietly dropped
  the guarantee.
- **`audio_playing()` is re-read in that switch** rather than using the frame's `speaking`. The
  repeat window is supposed to start when a message ENDS; with the stale flag it started the
  instant playback began.

**It could not be cleared from the box, either**, which is worth recording: the debug SQL
surface is read-only (correctly), and `DELETE /messages` deliberately keeps a message a panel
has not played — §5's rule protecting exactly this row. Both defences held and both pointed the
same way: the only way out was the firmware.

**Worth building next, and not bundled into this fix:** the box should stop re-offering a
message it has handed to the same panel many times without an acknowledgement. A panel that
cannot acknowledge should not be able to loop audio in a bedroom forever, whatever the reason —
that is a property of the box and it needs no OTA to take effect.

#### 10.4cx The instrument, finally wide enough to see through (0.2.91, 2026-09-23)

The confidence floor has been blocked since bring-up on one measurement: what does a CORRECT
decode score on this hardware. `speech.c` has computed it on every decode the whole time and
said so in its own comment. The ring that carries it to the box held **three** entries.

Three was sized for a bench, where the question is asked seconds later. The owner asks his from
another room, hours later, off a poll that runs every fifteen minutes — so a play session of
children shouting at a panel arrived as the last three things it thought it heard, and every
attempt to look at `dance` and `burp` found an empty ring.

**Twelve now, and consecutive identical decodes are collapsed into one entry with a count.**
The count matters more than the depth: a television repeating one word, or a child saying
"burp" eight times because it is not working, would flush the ring with eight copies of one
fact and push out everything that explained it. The repetition is the signature of a false
trigger, so it is kept rather than spent on slots.

**The box had to learn the new shape first, and both of them.** `TelemetryIn.heard` validated a
3-tuple; a 4-element entry would have 422'd, and a 422 telemetry is a FAILED report — the panel
keeps its crash ring and the reading simply never arrives, looking from the box exactly like a
panel with nothing to say. During an OTA one panel is on the old firmware and one on the new,
so the model accepts both arities. That is what makes a fleet upgradable one panel at a time,
and it is pinned by a test that reads `DECODE_MAX` out of `speech.c` — the same cross-package
coupling that shipped the `/api/jpanel` routes broken for a release because no test read both
ends.

#### 10.4cy The fleet view: the panel was never the thing that could not be read (2026-09-23)

The panel has reported richly for months — version, uptime, screen stage, blit counters and
their monotone totals, the largest free internal block, Wi-Fi drops and their reason, OTA
errors and retries, the crash phase, which of the three `esp_restart()` callers it was, what it
heard, where the last finger landed. Every one of those readings was added because a fault had
been invisible without it.

**And the only reader was `grep` over the box's structured log.** `POST /telemetry` said so in
as many words: *"Nothing is stored. These are a panel's own claims about itself, they are only
ever read by a human looking at a log, and a table would be a schema to migrate every time the
question changes."*

The objection was right — the report has gained `screen`, `heard`, `tap`, `restart_why` and
`panel_reset` since, and each one would have been a migration. **The premise underneath it was
not.** It assumed a human reading a log, and the owner has no terminal (CLAUDE.md #10). "Is her
panel alive, and did the update land" was a question only a shell could answer, which is what
*"just your update only has 0.2.88"* cost on a panel that had in fact updated forty minutes
earlier — the reading was in the box's log the whole time, forty lines up.

**`app.endpoint_status` (0210) keeps the answer without conceding the objection**: one row per
panel, upserted, with the report itself as `jsonb`. A field added to the report needs no
migration because there are no columns to add. `version` and `reported_at` are lifted out
because they are what every query sorts and filters on, and a panel with no row at all has
never reported — which is a *different* answer from having gone quiet, with a different first
move, and the fleet view keeps them apart rather than folding both into "unknown".

It is a SNAPSHOT, not a history. The `pmu_history` ring inside the report already carries the
only series anything has needed, and a growing table would be a retention question nobody has
asked.

**A panel may write only its own row, and the policy says so rather than the route.** These are
devices on children's walls authenticating with a key a four-year-old could hand to a visitor,
and this row is now the owner's only view of the fleet. A panel that could write its sibling's
could report that twin as dead, or as running a version it is not, and the one instrument he
has would be lying to him in the direction of "nothing to see". Four isolation tests in
`test_endpoint_status_rls_pg.py`; two of them fail when the policy is widened to any
`device_key`.

**What the card decides, beyond displaying.** Two missed reports before a panel is even called
late — one missed cycle is an ordinary Wi-Fi blip on a bedroom radio, and a fleet view that
cries wolf is ignored exactly when it matters. The screen stage leads every row, because a
sleeping panel stops blitting on purpose and `blit_ok` stops climbing: the exact signature of
the stalled render task that cost 0.2.44 a photograph from the owner to diagnose. A failing
update is the first concern listed, because a panel that cannot install retries every fifteen
minutes reporting the old version, which from the box is indistinguishable from a panel nobody
offered an update to.

**A related tidy, and the reason it belongs in this entry.** The label `/flash` writes onto a
panel's key is what marks a principal as a panel at all — for addressing, for the roster, and
now for this view. It was spelled out in `endpoint.py` and matched again in `jpanel.py`, with
nothing but a test reading one module's source from the other holding them together; a first
cut of that match lost every unit flashed *without* a name. `panel_label` and
`panel_display_name` are now defined once where the label is written and imported by the
readers. One definition cannot come apart; a test that two strings agree can only notice after
they already have.

#### 10.4cz The knob that took a quarter of an hour (0.3.06, 2026-09-25)

The owner, minutes after moving the brightness slider on a panel he had just powered on:
*"I set the panel brightness but apparently it's going to take 15 minutes... That seems
unnecessarily long considering when I send a message it's way faster."*

**He was right, and nobody had chosen it.** Settings were a PASSENGER on the update cycle.
`apply_settings()` ran at boot and then only after the manifest fetch at the bottom of
`app_main`'s loop, so the two cadences a panel actually has were fifteen minutes apart in the
wrong direction:

| path | cadence | where |
| --- | --- | --- |
| a message arriving | ~30 s | `jpanel.c` `POLL_EVERY_MS` |
| a knob the owner turned | **15 min** | `main.c` `CHECK_PERIOD_MS` |

The interval was reasoned about updates — *"a pushed update is not urgent, and an endpoint
hammering the box is a worse failure than a late rollout"* — which is a sound argument about
firmware and was never an argument about a slider. The code already knew it hurt:
`display.c`'s form guard says the difference is *"a child's afternoon"*.

**Three seconds, on the main task, in slices — and the update question rides the same poll.**
`cadence_slice_ms` (new, pure, host-tested) cuts the update period into poll-sized pieces
without moving where the period ENDS, so the telemetry post keeps the fifteen-minute schedule it
was designed for while a knob lands within three seconds.

The owner asked for the standardisation explicitly: *"I think even the update should go through
the same path standardize it to 3 seconds and if there's anything new it should be using it."*
So `GET /endpoint/settings` now also carries **`fw_version`** — what this box would serve — and
one round trip answers both "what are my knobs" and "is there new firmware". A panel learns
about an update within three seconds instead of at the end of a fifteen-minute manifest cycle.

**What deliberately did NOT move onto three seconds, and why each one is a measurement rather
than a preference:**

| stays slow | why |
| --- | --- |
| the 3.25 MB image | fetched only when the version actually CHANGES, never per poll |
| a FAILED install | backs off to the old fifteen minutes — see below |
| the telemetry post | an upsert, so no table grows, but §10.4bh reads a report at 6-7 s of uptime as PROOF OF A BOOT, and that only works while the interval is long |
| a second request for the version | it rides the settings response instead: every fetch is a fresh TLS handshake, and two requests answering one question each would cost twice the handshakes of one answering both |

**THE BACKOFF IS THE PART THAT MAKES THREE SECONDS SAFE, and without it this change would have
been a denial of service against the owner's own box.** `ota_apply` has no backoff and no attempt
cap — `ota_tries` is a reported counter, not a limiter — and the install fires whenever the
served version differs from the running one. At fifteen minutes a failing install retried four
times an hour. At three seconds it would retry **twelve hundred times an hour**, each pulling
3.25 MB: about **65 MB a minute per panel**, ~7.8 GB/hour across the pair, for a failure this
plan itself calls silent and *"indistinguishable from a panel nobody offered an update to"* —
and the first OTA this project ever attempted failed exactly that way. `cadence_retry_due` now
gates both install paths on `OTA_RETRY_BACKOFF_MS`, which is the old fifteen minutes: the
NOTICING is fast, the RETRYING is not.

Scope worth stating: the backoff bounds the WITHIN-SESSION retry rate, which is the risk the
fast poll created. A crash during an install still re-attempts on the next boot exactly as it
always has — that path predates this change, and fixing it needs the attempt written to NVS,
which is its own decision rather than a rider on a cadence change.

**The fetch stays on the main task, and that is why this is a slice loop rather than a flag.**
The obvious design — long-poll on the voice-post task, set a flag, let the main task apply it —
was the owner's own suggestion and is the right shape in general. It is not the cheap shape
here. `apply_settings` walks into the codec's I2C registers through `audio_set_levels`, and
§10.4al is the panic that happens when that runs anywhere but the main task. Slicing the sleep
the main task was already taking needs no cross-task signal at all: same call, same task, only
more often.

**Why not true push.** One measurement decided it, and it was not available from reading the
code:

- Both panels report `int_largest` — the largest free INTERNAL DMA block — at **31 KB**, on a
  device where `report()`'s own comment says internal fragmentation *"has explained the fault
  twice"*. That is also what rules out true push for now: a long-poll needs its own task,
  because `jpanel.c`'s ticks every 250 ms to service taps and the speaker-finished
  acknowledgement and cannot block for fifteen seconds — and a second concurrent mbedTLS
  session against a 31 KB largest block is the ESP-SR-versus-radio fault (§10) wearing new
  clothes. Take the long-poll when telemetry shows `int_largest` holding under the three-second
  rate. The number is in every report, so the evidence arrives without anyone instrumenting
  anything — and if it sags, `POLL_PERIOD_MS` is the one number to raise.

**One guard added, and it is load-bearing now in a way it was not before.**
`display_set_brightness` raised its pending flag unconditionally, which at fifteen minutes was
invisible and at three seconds is twenty backlight writes a minute for a value that had not moved.
Harmless on the glass — `apply_brightness` recomputes through `screen_level`, so re-asserting
while dim writes the DIMMED level rather than waking the screen — but the form guard beside it
is the one that matters: it was protecting a child's gesture from being undone four times an
hour, and it is now protecting it twenty times a minute.

**A second flaw the diff review caught, and it only exists because of the new rate.** `joined`
is re-read only AFTER the sleep, so a router that goes down mid-period leaves `apply_settings`
being called on a dead network — and each failure blocks the main task for `HTTP_TIMEOUT_MS`
(15 s) against a 3 s slice, stretching the period well past its hour and delaying the
manifest fetch and the telemetry post on precisely the panel that most needs both. At fifteen
minutes this could not happen, because there was only ever one attempt. `apply_settings` now
answers whether the box replied, and one failure stops the asking for the rest of the period —
which puts an offline panel back on exactly the single long sleep it had before.

**The host suite caught its own first draft.** The first version of the no-drift test used the
shipped constants, and fifteen minutes is a whole number of ten seconds — so it passed against
a `cadence_slice_ms` with its tail case deleted. It also subtracted before checking, so an
overshooting slice underflowed `uint32_t` and the test hung instead of failing. Both are fixed
in the committed version: the period is deliberately ragged, and the fit is asserted before the
subtraction.

#### 10.4da The tick and the cross (0.3.07, 2026-09-25)

The owner: *"I want to change when we are recording, how the gui looks ... I want icons that
are green check and red x that are kind of large in the bottom third ... Green check will
finish when pressed and send, red x cancel."* For `hey fish` and for messages to dad or a
sister — the two turns that run hands-free.

**What a recording had before this was three silent exits and one gesture.** Going quiet sent
it, saying nothing dropped it, the cap sent what there was — and a touch ANYWHERE cancelled and
discarded. That last rule was the owner's own (*"when it's listening, if I touch the screen it
should stop and discard"*) and it was right while it was the only way out: a hands-free listen
has to be escapable by someone who is not going to speak, and a finger is the one input always
available.

**It is the wrong rule the moment there is somewhere deliberate to press.** The finger that
means "send this" is then one bad aim from the finger that destroys it, and the reader is four.
So: the cross cancels, the tick sends, and **anything else on the glass does nothing** —
including the pet, which stops being a cancel button while a child is talking to it.

**Going quiet still sends,** and that was a decision rather than an omission. A voice assistant
that needs a button press every time is not hands-free; the tick means *"I am done, do not wait
it out"*. A held listen never reaches any of this — it ends on the release of the finger that
started it, so a tick would be a second way to finish a gesture that already has one.

**A tick pressed into silence does not send.** The hush branch already refuses to send a room
nobody spoke into, and the one exit that skips the silence check had to refuse too — otherwise
the shortcut becomes the way six seconds of a bedroom reaches dad. It drops with the same
out-loud cue, because a child who pressed the tick and heard nothing has been told it went.

**`confirm.c` is pure, and that buys two things a `display.c` static could not.** The hit
geometry is host-tested — the assertion that matters most in the file is that the two targets
**cannot overlap**: 184 px between centres against an 82 px reach leaves a 20 px dead band, so
a finger that lands between them does NOTHING rather than picking whichever circle won.
Verified by widening the reach until they touched and watching the test fail. And because the
drawing takes only a framebuffer, the same code that runs on the panel can be RENDERED on a
host — the half of this feature a hit test cannot check was looked at before it went near a
panel.

**The target is deliberately larger than the disc** (82 against 56), on the pop-up's own
lesson: *"a four-year-old aiming at a small target with an excited finger is a miss."*

**Anchored to the overlay band, not the frame.** `over_h` is the frame on a portrait panel and
the SQUARE on a side-mounted one, so measuring up from its bottom puts these in the bottom
third either way — the same rule the caption and the label were moved to obey. A version that
hardcoded the frame would put them off the edge of a turned panel, where a child would press
glass that does nothing and a message would have no way out but silence. Pinned by a test.

#### 10.4db The tick and the cross were 289 pixels from the finger (0.3.08-0.3.09, 2026-09-25)

0.3.07 drew the icons correctly and could not be pressed. The owner: *"I tried to do a tell
Dad, and the icon show up for check mark and x. But they don't respond when I touch it"* — and,
crucially, *"That might be the reason why I couldn't click on the version to name in the top
left also"*, which is the same fault in a control that predates this one.

**THE PANEL WAS UPSIDE DOWN, AND THE HIT TEST WAS THE ONLY ONE THAT DID NOT KNOW.** Anything
drawn BEFORE `flip_frame` — the pop-up, the repeat icon, the label, the caption, and now the
tick and the cross — is written in frame order and then reversed, so a touch has to be reversed
the same way before it is compared (`tap_to_overlay`). `confirm_hit` was given raw frame
coordinates. Measured on the box: a press on the tick, drawn at frame (276,368), arrives as
(91,79) — **289 px away**, on a 82 px target.

**THE TAP MARKER IS WHY THIS LOOKS IMPOSSIBLE FROM THE OUTSIDE, and it is the clue that solved
it.** The owner: *"there's a little yellow circle that shows up where I click and I'm pretty
sure that yellow circle showed when I was ... clicking the icons and they didn't respond."* The
marker is drawn AFTER the flip and uses `s_fig` directly, so it sits under the finger by
construction. A panel therefore *looks* like it is tracking touch perfectly while every
pre-flip target on it is a half turn away. Every other symptom fell out of the same cause: the
label dead (`label_hit` had the identical gap), pokes still working (`face_zone` takes
`s_upside_down`), and the pop-up fine (it already used `tap_to_overlay`).

**Fixed by resolving the overlay pair ONCE, beside the frame pair**, and pointing every overlay
hit test at it — including `label_hit`, whose fault was older than this release and which
`tap_to_overlay`'s own comment had already predicted by naming the label.

**A DIAGNOSIS THAT NEEDED A USB CABLE, AND SHOULD NOT HAVE.** The miss was silent on the glass
by design — a pet that twitches while a child is talking to it invites the next poke — but it
was also silent in the LOG, which was not a design decision, just an omission. The only symptom
available to an owner with no terminal (CLAUDE.md #10) was "it does not respond". It now prints
both pairs on every miss: agreeing puts the fault in calibration, disagreeing puts it in the
flip. One line would have answered this without anyone finding a cable.

**CONFIRMED ON HARDWARE, and the shape of the confirmation is the diagnosis.** With the flip
fix not yet deployed, the owner tested all four orientations: *"I tried it in one orientation
and it worked rotated 90° and it worked rotated another 90° and it didn't work."* Three of four
is exactly what a missing `tap_to_overlay` predicts — quarters 0, 1 and 3 need no correction and
only quarter 2 does. *"But the little yellow circles show up everywhere where I press"*, which
is the marker being drawn after the flip in every orientation, as designed, and is why the
panel looks like it is tracking touch correctly in the one orientation where nothing can be
pressed.

**AND THE TICK WAS SILENT WHILE WORKING, which is its own bug (0.3.09).** The owner: *"there is
a sound effect when hitting cancel, but not the check mark"* — with four messages arriving on
the box from that panel in the same four minutes, so the target was never dead. `CUE_SENT`
exists and is played, but only on `JPANEL_SENT`, when the BOX confirms, a network round trip
after the press; the cross answers instantly with `CUE_STOP`. The two targets therefore felt
different in the hand. The tick now sounds for the FINGER first and lets the outcome follow —
the rule the pop-up already had, and whose own comment warns that a control answering with
silence is the one a child gives up on.

**A WRONG TURN WORTH RECORDING, because the evidence was read carelessly rather than being
ambiguous.** The first reading of the two logged taps assumed they were left-then-right and
concluded only Y was inverted — pointing at calibration, which the boot log supported (neither
`touch calibration loaded` nor `stored touch calibration rejected` prints, so nothing is
stored). The owner had said right-then-left. With the stated order BOTH axes invert, which is a
half turn, which is the flip. The panel is genuinely uncalibrated and that is still worth doing
one day; it was never this bug, and calibrating while upside down would have written a 180
degree error into NVS — wrong in the other three orientations and double-corrected the moment
the code was fixed.

#### 10.4dc The raw decode, in telemetry rather than down a cable (0.3.10, 2026-09-26)

*"Tell sister" isn't recognized* — with `tell dad` firing and both phrases registered. The owner
then asked the right question: *"Can you take out some research on how this actually works for
the recognition. I feel like we're not adequately understanding the issue."* He was right; this
section had been reasoning from its own folklore.

**WHAT THE PRIMARY SOURCES SAY, AND WHERE `vocab.c` WAS WRONG.** MultiNet7 English decodes
**phonemes, not words**: the shipped model's own `vocab` file is a language model over the
single-letter phoneme classes `tool/multinet_g2p.py` emits (`D`, `Z`, `ST`, `cN`, `eR`…), with
log-probabilities. The `tool/README.md` line *"for English, words are used as units"* is
**MultiNet6** — a different model from the one this firmware builds
(`CONFIG_SR_MN_EN_MULTINET7_QUANT`).

So the claim in `vocab.c` that two phrases sharing a first word *"split the confidence between
them"* does not describe this decoder. A shared prefix is ordinary. Running Espressif's own
alphabet over CMUdict shows the two phrases diverging completely after the carrier:

| phrase | phonemes | encoded |
| --- | --- | --- |
| tell dad | `T EH1 L / D AE1 D` | `TfL DaD` |
| tell sister | `T EH1 L / S IH1 S T ER0` | `TfL SgSTk` |

What stands out is not the shared `TfL` but what "sister" is made of — `S-IH-S-T-ER`, sibilants
around a weak vowel, ending in schwa-r — against `D-AE-D`, voiced plosives around a strong open
one. On a far-field mic behind noise suppression those are not equally survivable.

**AND WE ARE ON THE DOCUMENTED FALLBACK PATH.** Espressif: *"use `tool/multinet_g2p.py` to do
the Grapheme-to-Phoneme conversion"*, and if that step is skipped *"an internal
Grapheme-to-Phoneme tool will be called at runtime"* with potential accuracy reduction. There is
a dedicated API for the correct path —
`esp_mn_commands_phoneme_add(id, string, phonemes)`, whose own doc says to use that tool — and
`speech.c` calls `esp_mn_commands_add(i, phrase)`. **All 48 commands go through the fallback.**

**MEASURED, from the panel the owner actually speaks to** (which is the one WITHOUT a cable):
`tell dad` at p=17, `tell sister` never once in the ring, and a run of **212 consecutive
non-matches** beside it. `mic_peak` 8676, so the microphone is hearing him; `alc` still
`00 already-off`.

**WHICH IS WHY THIS COMMIT IS A TELEMETRY CHANGE AND NOT A FIX.** Two candidate causes remain
and they need opposite remedies: the audio never carried the sibilants, or it did and they
scored below something else. The decoder's own `raw_string` separates them in one field — and
it was only ever visible on a USB console, on a panel that has no cable in it, which is the
CLAUDE.md #10 failure this section keeps rediscovering. The ring now carries it, on the report
that was already being sent.

Two defects caught while writing it: the loop guard reserved 64 bytes against an entry that can
now reach ~69, which is the unterminated-JSON failure §10.4 already paid for once; and `raw`
lands inside a JSON string that nothing downstream escapes, so it is sanitised on the way in.

#### 10.4dd A stop and an again, at the tick's size (0.3.11, 2026-09-26)

The owner: *"when panel is playing back a message from my pwa, show a big stop icon similar to
the x when recording. And when we have the again have it the same size icon but with like a
repeat and big like that."*

Both replaced text controls sized for an adult reading them: a `"N MORE  TAP TO STOP"` bar and
an `"AGAIN"` word box. `draw_repeat`'s comment had defended the word — *"a hand-plotted circular
arrow at this size reads as a smudge"* — and that objection was entirely about SIZE. True of a
52 px corner box, not of a 112 px disc. The readers are four and cannot read "AGAIN" anyway.

**CENTRED, because unlike the tick and the cross these are ALONE.** Two targets need a dead band
between them so a miss cannot pick the wrong one; one target belongs where the thumb already is.
Same disc, same reach, same bottom third, same overlay band — so they follow a turned panel for
free, and `confirm_hit_centre` is the same circle test.

**A SQUARE FOR STOP, NOT AN X.** The cross already means "throw this away" on the recording
screen, and a control that stops playback must not read as one that destroys the message — it
stays in the queue either way. Square is also what every transport control these children have
already seen uses.

**AGAIN MOVES FROM THE TOP-LEFT CORNER TO THE BOTTOM THIRD**, where everything else a finger is
meant to press now lives. A control whose location has to be learned separately is one a child
will not find.

**TAP-ANYWHERE STILL STOPS A RUN, deliberately unlike the recording screen.** That rule was
removed from recording because a mis-aim there destroys a message; stopping playback has no
destructive neighbour to mis-aim into, the message survives, and it is the gesture the children
already know. The icon adds the discoverability `draw_run`'s own comment asked for ("a way out
they can SEE") without taking away the forgiving one.

**THE COUNT SURVIVED, AND MOVED TWICE.** "How many more" is the question a child sitting through
four messages actually has, and a digit answers it where the word never could. Drawn above the
disc first, where white numerals landed on the pet's own light body and all but vanished in the
render; it sits beside the disc now, on the black margin, in the slot the cross occupies on the
recording screen. Caught by looking at the picture before it shipped, which is the whole reason
`confirm.c` renders on a host.

#### 10.4de Every phrase converted at build time, not at run time (0.3.12, 2026-09-26)

The research in §10.4dc found this firmware on the path Espressif warn about, and the owner's
call was to take the documented one: *"let's go ahead and do Precomputed phonemes. Switch all 48
commands from esp_mn_commands_add to esp_mn_commands_phoneme_add(id, string, phonemes)."*

**WHAT CHANGED.** `vocab_t` carries a `phonemes` string beside every phrase, and `speech.c`
registers through `esp_mn_commands_phoneme_add` — the API whose own documentation says to use
`tool/multinet_g2p.py`. 47 of 48 entries arrive already converted. `tell sister` is `TfL SgSTk`
and `tell dad` is `TfL DaD`.

**THE ONE EXCEPTION IS STRUCTURAL, NOT AN OVERSIGHT.** The wake phrase carries the pet's NAME,
the owner can change it from the PWA, and a name that does not exist at build time cannot have
been converted at build time. That entry keeps the runtime converter — the path every entry was
on until now — rather than being refused, and the host suite pins that it is the ONLY one.

**HOW THE STRINGS WERE PRODUCED, and where the honest gap is.** `g2p_en`, which
`multinet_g2p.py` wraps, could not be installed here — its `distance` dependency will not build
— so the encodings come from **CMUdict** run through that tool's own alphabet map. That is the
same source `g2p_en` uses for in-vocabulary words; its neural net only serves words CMUdict does
not have. Exactly one phrase hit that case: **"peekaboo"**, which is absent from CMUdict and is
therefore composed from `peek` + `a` + `boo`, all three of which are present. Composed, not
invented — but worth naming, because it is the one string here that was not looked up whole.

**THE STRUCT PUTS `phonemes` SECOND ON PURPOSE.** An entry that forgets it puts an enum where a
`const char *` belongs and fails to build. A table of 48 transcribed strings stays honest only
if omission is a compile error rather than a silent NULL that drops back to the runtime path.

**AND WHETHER IT FIXES "TELL SISTER" IS A MEASUREMENT, NOT A CLAIM.** The evidence that led here
is that `tell dad` fires at p=17 while `tell sister` has never once appeared, with 212
consecutive non-matches beside it, on a microphone reading `mic_peak` 8676 with `mic_agc` still
off. Better conversion is the vendor-documented improvement and it was free to take; it is not
proof the sibilants were surviving the microphone in the first place. `raw_string` (§10.4dc)
lands in the same deploy to answer that, and `mic_agc` is still a toggle nobody has turned.

Four host cases pin the data, each verified to fail against a deliberate break: every character
is in the g2p alphabet, exactly one entry converts at runtime and it is the wake phrase, a word
shared between phrases encodes identically, and the two send phrases are literally what was
measured.

#### 10.4df The last phrase on the runtime path, converted on the box (0.3.13, 2026-09-26)

§10.4de left exactly one entry converting at runtime, and named it structural: the wake phrase
carries the pet's NAME, the owner changes that from the PWA, and a name that does not exist at
build time cannot have been converted then. The owner's read was that the exception did not have
to stand — *"I feel like you could have the server box be able to construct them on the fly"* —
and then scoped it: *"Yeah I would keep your pre-generated static ones and only generate the pet
name."* Which is the right scope: 47 phrases are fixed and belong in flash; one is not.

**WHAT CHANGED.** `backend/src/jbrain/g2p.py` carries the value half of `tool/multinet_g2p.py`'s
alphabet and looks words up in CMUdict. `GET /endpoint/settings` gained `pet_name_phonemes`
beside `pet_name`, so the name and its pronunciation arrive in one answer on the poll the panel
already makes every three seconds — no second TLS handshake, which matters on a panel whose
largest free internal block is 31 KB. `vocab_set_name(name, phonemes)` stores both, and the
`VOCAB_LISTEN` row points at both buffers. The carrier word's phonemes stay in the firmware
(`hd`, i.e. `HH EY1`): the box sends the NAME alone, because "hey" is `vocab.c`'s word and a box
sending the whole phrase would be encoding a decision it cannot see.

**IT REFUSES RATHER THAN REPAIRS, on both sides.** A phrase half-converted is worse than one not
converted at all, so `phonemes_for` returns None if ANY word is unknown, and the panel drops a
phoneme string whole if it carries a character outside the alphabet or will not fit. Both land
on the same fallback: the runtime converter, which is where this phrase already was. The case
that actually bites is renaming FROM a name the box could pronounce TO one it cannot — the old
phonemes must be cleared, or the panel listens for the sound of the previous name while showing
the new one, and there is a host test for exactly that.

**AND A BUG IT UNCOVERED, which was the more serious half.** Building this meant testing that the
name reaches a panel, and it does not: `panel_settings` read `app.endpoint_panel` under
`ctx_for(principal)`, which carries a principal id and kind and NO subject pin, while
`endpoint_panel_own` (migration 0212) grants a panel its row on `principal_kind = 'device_key'`
AND `subject_id = app.subject_id`. The policy matched nothing, so **every panel has been served
the default appearance since 0212** — empty name, `ostrich` — however the owner set it. The
symptom is that renaming the pet from the PWA did nothing at all, and choosing the robot did not
survive a reboot, both of which read as firmware faults and are not.
`tests/integration/test_endpoint_panel_rls.py` passed throughout: it builds the pinned context by
hand and proves the POLICY works. Nothing checked that the ROUTE built the same context, which is
the gap. `panel_context()` is that context — deliberately not `device_context`, which carries the
`location` domain scope a panel has no use for — and the new integration tests assert the owner's
`robot` arrives rather than the default `ostrich`, because a test pinning `ostrich` would have
passed for the whole life of the bug.

**WHAT THIS DOES NOT FIX, stated so the next step is not mis-scoped.** CMUdict is a dictionary,
not a model. Measured: `fish`, `blink`, `merc`, `nessa` and `bluey` are present; **`elora` and
`lydian` are NOT** — the twins' own names, and the obvious thing to call a panel. An invented
name is not a word, gets "", and converts on-chip exactly as before. Closing that needs a real
G2P with out-of-vocabulary handling (`g2p_en` carries a neural net for it, and could not be
installed here — its `distance` dependency will not build), which is also what per-panel sibling
names ("tell Elora") would need. Worth doing deliberately rather than as a rider on this.

**AND A SECOND BUG, CAUGHT BY CI ON THIS PR RATHER THAN BY A PANEL.** §10.4dc added a fifth
field to each telemetry decode — the decoder's RAW phoneme string, the only thing that can
separate "the microphone never carried it" from "it was heard as something else" — and widened
`main.c`'s format string without widening `TelemetryIn.heard`, whose union still ended at four.
Every report from a panel on 0.3.10 or later would have 422'd, and a 422 is a FAILED report: the
crash ring kept rather than cleared, the reading never arriving, and from the box a panel that
looks like it had nothing to say. The first casualty would have been the measurement that field
exists to take. Nothing had shipped it — 0.3.10 to 0.3.12 are unmerged — so no panel was ever
affected. `TestTheHeardRingCrossesThePackageBoundary` is the test that caught it, from the
format-string side; the model now has its own case so the two cannot drift apart from the other
side either. The union's own comment already described this failure and anticipated the wrong
direction: an old panel against a new box, rather than a new panel against a box nobody updated.

Backend: seven unit cases on the converter, including one that cross-checks it against **every
phrase already in flash** — a consistency check rather than independent verification, and it
catches the thing that actually happens, a hand-edit to `vocab.c` or to the alphabet leaving the
wake phrase encoded in a scheme the other 47 are not. Three integration cases on the route, two
of which fail against the pre-fix context. Six host cases on the panel's half, each verified
against a deliberate break.

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

- **The panels will eventually present as EGGS** (owner, 2026-09-21): interact with one enough
  times over enough minutes and it hatches into a baby that grows up over ~2 days. Iceboxed as
  `../proposed/PET_LIFECYCLE_PLAN.md` rather than scoped here, because the firmware is the
  cheap half — a baby is a `growth` float through `face.c`'s existing per-form constants, plus
  one extra silhouette for the ostrich (whose neck and legs ARE its silhouette, so scaling them
  down gives a small ostrich rather than a chick). The expensive half is `jpet/`: there is no
  age or stage in `pet_state` today, and it holds one pet per principal where two twins need
  two eggs.

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
