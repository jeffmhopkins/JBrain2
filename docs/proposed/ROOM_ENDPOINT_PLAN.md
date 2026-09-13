# Room endpoints — the box's face and ears on a small AMOLED satellite

> **Status:** Proposed (icebox) · **Last verified:** 2026-09-13

**Status: proposed / icebox.** Nothing built, no roadmap slot — but unlike most of this
folder, **the hardware is ordered** (two units), so this is a plan against a real device
rather than a thought experiment. Supersedes `../archive/DITOO_PLAN.md`, which chased the same
goal through Bluetooth and paid for it; **the Ditoo itself is out of the picture** — cancelled,
not retained as a speaker — and that doc is archived for its findings, not its design. When
picked up, reconcile with the `CLAUDE.md` non-negotiables, get a `docs/ROADMAP.md` slot, and
promote out of `proposed/`.

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
  goal.
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
| **W1** | **Bench bring-up + decisions.** Flash Waveshare's sample, confirm display/mic/speaker, settle §4.1 transport, §4.3 toolchain, and get **OTA** working. | The wave where the hardware votes. Bench unit only. |
| **W2** | **Protocol + device identity.** N-endpoint addressing on the shipped `device_key` model, `endpoint_url`/broker config defaulting to empty so the feature is simply absent when unset (the `sdr_url` pattern), Settings → Endpoints. | No new auth model. |
| **W3** | **Display path.** Frame/scene protocol, the 23 px grid renderer, clock + box-vitals + notification cards. Needs a `docs/mocks/` artboard first, per `DESIGN.md`. | 368×80 caption strip is part of the design language. |
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
