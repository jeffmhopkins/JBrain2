# Divoom Ditoo on the box — feasibility + build sketch

> **Status:** Superseded 2026-09 · **Superseded-by:** `../proposed/ROOM_ENDPOINT_PLAN.md`

> **The device is out of the picture entirely** — the owner cancelled it rather than keep it
> as a Bluetooth speaker, so nothing here is pending. The goal it chased (§0.2 — pixel art,
> wireless, speaker and microphone in one small object) is met instead by two Waveshare
> ESP32-S3-Touch-AMOLED-1.8 endpoints, which carry no Bluetooth at all and so delete §4
> outright, along with the range limit, the one-link-at-a-time contention with the owner's
> phone, the reverse-engineered protocol and the vendor-OTA risk.
>
> Archived for its findings rather than its design. Four outlive it:
> **§1.1** the USB question, closed with evidence; **§4.2** Bluetooth sockets work only in the
> initial network namespace, so a BT sidecar cannot use the `internal: true` egress lock that
> `radio`/`render` rely on; **§4.3** what `update-inner.sh` can and cannot do to the host from
> the PWA path (it writes files, loads/unloads modules and unbinds drivers — it cannot `apt`);
> and **§5** the correction that `bluez-alsa` needs no host sound server, which is the general
> fact, not a Ditoo one. **§0.2** also holds the 2026-09 survey of small pixel/voice hardware.

This doc records the design as it stood when the device was dropped; it is not maintained.
Section numbers referenced above are unchanged from the final revision.

## 0. The verdict

**Yes — and not just the panel: display, speaker and microphone all come off one sidecar.**
It is *not* a drop-in like the SDR was — §4 is real work — but that work is bought once and
buys the whole device (§5 corrects an earlier draft that scoped audio out). Three things
gate it:

| Gate | Status | How we find out |
|---|---|---|
| The box has a **Bluetooth Classic radio** | **Unknown — must probe** | Free: the shipped `GET /usb` supervisor probe already lists it (§4.1) |
| The speaker sits within **~10 m** of the box | Owner knows | Physical placement |
| Owner accepts an **unofficial, reverse-engineered** control protocol and that the **phone app must stay off it** | Owner decision | §6 |

If the first gate fails, everything below is dead until a ~$10 USB Bluetooth dongle is
plugged in — which is fine, but it is a physical act, and the owner runs this box
remotely (`CLAUDE.md` #10).

### 0.1 The hardware choice is the biggest lever in this plan

Worth stating before anyone builds §7: **almost all of the cost here is the wireless
link, not the pixels.** A small **Wi-Fi** panel with a local HTTP API on the LAN deletes,
in one stroke, the sidecar container, BlueZ, the network-namespace constraint (§4.2), the
host module loads (§4.3), the pairing UI (§4.4), the ~10 m range limit and the
one-link-at-a-time contention with the owner's phone (§6). What survives is a pinned-URL
client next to `sdr_url` and the renderers — **D2-D5 without D0 or D1**, and no wave where
the hardware gets a vote.

Concretely, if the device is being chosen rather than already owned:

| Device | Canvas | Link | What it costs us |
|---|---|---|---|
| **Divoom Pixoo 16** | 16×16 | 2.4 GHz Wi-Fi, local HTTP API | A client + renderers. No sidecar at all. |
| **Divoom Pixoo-Max** | 32×32 | Wi-Fi + BT | Same, with a roomier canvas. |
| **Ulanzi TC001** + AWTRIX 3 | 32×8 | Wi-Fi, HTTP **and MQTT**, open firmware | Same, and it can ride the **`mqtt` profile broker this repo already ships**. Wrong aspect ratio for JPet. |
| **Divoom Ditoo** (this plan) | 16×16 | Bluetooth Classic SPP only | Everything in §4 — but it is the **only** row with a speaker *and* a mic (§0.2). |

Two caveats that cut the other way. Divoom's local API is officially documented only for
the Pixoo-64 line — support on the 16/32 is **community-verified**, so confirm the unit is
a Wi-Fi revision (it gets a LAN IP the app will show you) before relying on it. And any
Divoom device is a **cloud-attached appliance on the LAN**: it talks to Divoom's servers,
which on a box built around domain firewalls deserves a firewall rule or an IoT VLAN, not a
shrug. The Ulanzi/AWTRIX route is the only one on that table with no cloud in it at all.

None of this is device-specific above the renderer layer, so the panel can be swapped later
without touching D3-D5.

### 0.2 The owner's real spec: pixel art + wireless + speaker + mic, in one small object

That is four requirements at once, and it narrows the field to almost nothing. Surveyed
2026-09-13:

| Candidate | Pixels | Wireless | Speaker | Mic | Open/local control |
|---|---|---|---|---|---|
| **Divoom Ditoo** | 16×16 | BT Classic | 15 W | yes (HFP) | reverse-engineered SPP |
| Divoom Pixoo 16 / Pixoo-Max | 16×16 / 32×32 | **Wi-Fi** | — | — | local HTTP API |
| Ulanzi TC001 + AWTRIX 3 | 32×8 | **Wi-Fi** | buzzer only | — | HTTP + MQTT, open firmware |
| HA Voice PE & voice satellites | LED ring | **Wi-Fi** | yes | mic array + AEC | open, but HA-shaped |
| Meterbit Pixlpal | 128×64 | **Wi-Fi** | line-out | MEMS mic | fully open — but ~11″ and crowdfunding |
| DIY ESP32-S3 + WS2812 + I2S | any | **Wi-Fi** | I2S amp | I2S wideband | totally yours |

**No shipping, small, off-the-shelf product hits all four with an open API.** The Ditoo is
the closest thing that exists — which is presumably why it got bought — and its price is
that control is Bluetooth and unofficial. Every Wi-Fi panel in that table *loses the speaker
and mic entirely*, so §0.1's advice to swap for a Pixoo does **not** serve this goal and is
withdrawn for it.

The relevant point, corrected in §5: one sidecar covers **all three** of display, playback
and capture, because `bluez-alsa` needs no host sound server. So the §4 plumbing — built once
— buys the whole device, not just the panel. That is a much better trade than it looked.

**What the box already has.** The voice loop is largely built, and today its endpoint is a
**browser**: `deploy/wall/pet.html` opens the room mic with `getUserMedia`, runs continuous
wake-word listening, and does echo-cancelled **barge-in** (it subtracts the pet's own TTS and
cuts it off when a child talks over it); read-aloud is served by `tts-stt` (Kokoro) and
fetched same-origin by the kiosk; whisper.cpp for STT sits in that same container. One honest
gap regardless of hardware: the wake word and recognition run on the **browser's** Web Speech
API (`frontend/src/screens/speech.ts`), i.e. Google's cloud in Chrome — on a box built for
privacy that wants replacing with the on-box whisper the stack already ships.

**The one thing the Ditoo cannot be talked into** is a good full-duplex endpoint: no hardware
AEC, and HFP capture drops playback to narrowband while the mic is open (§5). If audio
*quality* outranks having one cute object, a DIY ESP32-S3 build (wideband I2S mic, I2S amp,
WS2812 matrix, Wi-Fi, no link contention, no range limit) is the only route that hits all
four *and* integrates cleanly — at the cost of building it.

---

## 1. What the hardware actually is

**Divoom Ditoo** (the original, ASIN `B07YWWYK86` — *not* the Ditoo Pro):

- 15 W Bluetooth speaker, **256-pixel (16×16) RGB front panel**, mechanical keyboard with
  RGB backlight, alarm clock / white noise / Pomodoro, FM radio, TF-card playback, a mic
  for the music visualiser, USB-C **for power**.
- **No Wi-Fi, no Ethernet, no network stack.** This is the single most consequential fact
  and the thing most "Divoom API" material on the web gets wrong for this device: Divoom's
  own documented local HTTP API, and the mature libraries built on it, target the
  **Pixoo-64**, which *is* a Wi-Fi device. The Ditoo has none of that.
- The only control channel is **Bluetooth Classic**, and specifically **SPP over RFCOMM**
  — a serial link riding alongside the audio profile. The command set is
  **reverse-engineered from the Divoom app**, not published.

So the integration is: *"drive a serial device that happens to be attached over Bluetooth
Classic, from a box that currently has no Bluetooth stack at all."*

### 1.1 "Can't we just use the USB port?" — no, and it's worth knowing why

This is the first question anyone asks, because a wired USB device would collapse this
whole plan into the SDR pattern: `/dev/bus/usb` passthrough, an ordinary bridge-network
container, the `internal: true` egress lock intact, no BlueZ, no pairing, no range limit,
no contention with the phone. Every hard part in §4 exists *only* because the link is
wireless. So it is the right instinct — it just isn't available on this device.

Three independent lines of evidence, and they agree:

1. **Divoom says so.** Their own FAQ states they offer no Windows/Mac desktop application
   at all — the app is phones and tablets only — and describes the Ditoo's USB-C solely as
   the charging port. A PC can use the Ditoo as an ordinary Bluetooth *speaker* through the
   OS; anything touching the panel goes through the phone app.
2. **Every reverse-engineered implementation is Bluetooth-only.** `hass-divoom`,
   `go-divoom` ("native RFCOMM everywhere"), `divoom-ditoo-pro-controller`,
   `node-divoom-timebox-evo` — all RFCOMM/SPP, none with a USB transport. The strongest
   evidence is `esp32-divoom` itself: a whole second microcontroller built to bridge Wi-Fi
   to Bluetooth. Nobody builds that if a USB cable works.
3. **Even the Pro's USB-C wouldn't help.** The Ditoo *Pro* does advertise USB-C to a
   laptop or desktop — but as an **audio** path ("more ways to play music"), i.e. USB
   Audio Class. The pixel protocol is defined over SPP regardless of model, so USB on the
   Pro buys sound, not pixels. Ours is the base model, where USB-C is power only.

Treat SEO content claiming the Ditoo "connects to your computer via Bluetooth or USB cable"
as the marketing mush it is: it is conflating charging, and using the speaker as a BT audio
sink, with control of the display.

**What USB *can* usefully do here** is supply the box's end of the link: a ~$10 USB
Bluetooth Classic dongle closes the §4.1 presence gate if the box's combo card turns out to
have no usable BT side, and a USB extension cable is the cheapest way to move the radio a
few metres closer to the speaker. Neither changes the netns constraint in §4.2 — the
dongle still speaks Bluetooth — but both are real answers to *"no radio"* and *"out of
range"*.

## 2. Why this is harder than the SDR was

The SDR (`docs/plans/SDR_RADIO_PLAN.md`, `deploy/sdr/`) is the box's only existing
hardware integration, and it is the right template — but three of the four properties
that made it easy don't hold here.

| | RTL-SDR | Ditoo |
|---|---|---|
| Presence check | Free — sysfs, no passthrough (`supervisor/usb_devices.py`) | Free for the *radio*; the speaker itself is only visible via a BT scan |
| Getting at it from a container | `devices: /dev/bus/usb` — one line of compose | Bluetooth sockets **only work in the initial network namespace** (§4.2) |
| Host packages needed | None | A BlueZ stack must exist *somewhere* (§4.3) |
| Pairing / persistent association | None | Yes, and it has to be driveable with no terminal (§4.4) |
| Isolation | `radio` network, `internal: true` — provably egress-free | **Not available** (§4.2), so isolation must be bought another way |

## 3. What it would be *for* (ranked by value / effort)

Worth deciding before building, because the ranking changes the design:

1. **A second body for JPet.** The pet is already server-authoritative with an in-process
   broadcaster fanning state to every subscribed surface
   (`backend/src/jbrain/jpet/broadcast.py` — the Wall and the phone Control screen are
   just two subscribers). A 16×16 pixel panel is *the* native medium for a pixel pet.
   Highest delight, and it introduces no new concept — one more subscriber.
2. **An ambient face for the box.** The Wall (`deploy/wall/`) already computes GPU busy %,
   RAM, APU power and load from host `/proc` and `/sys`. Rendering a glanceable version of
   that into 256 pixels is a design problem, not an architecture problem.
3. **Owner notifications.** `backend/src/jbrain/notify/bus.py` already fans owner
   notifications to subscribed surfaces; a pixel card for "a run finished / a proposal
   needs review" is a small renderer on an existing bus.
4. **A workflow action.** The engine has a real action registry
   (`backend/src/jbrain/workflow/registry.py:ACTION_SPECS`) and a scheduler, so
   "show the morning face at 07:00" is a row, not code.
5. **An agent tool.** Lowest value, highest care: a model that can light up a device in a
   shared room is a physical side-effect surface. If it lands at all it should be
   owner-scope only, rate-limited, and content-fenced (§6).

**Audio belongs on this list too**, and near the top when the goal is a room endpoint: the
same sidecar gives the box a voice and ears in that room (§5). It is listed separately only
because it is a different kind of feature from a renderer.

## 4. The four hard parts

### 4.1 Does the box even have a Bluetooth radio? (blocking, and free to answer)

The box is a **GMKtec EVO-X2 / Ryzen AI Max+ 395** (`docs/runbooks/STRIX_HALO_SETUP.md`).
Machines in that class ship a Wi-Fi 6E combo card whose **Bluetooth side enumerates as a
USB device** on an internal bus — MediaTek (`0e8d:…`) or Intel (`8087:…`) typically. That
means the existing probe answers it with **no new hardware access at all**: the supervisor
already reads the host's `/sys/bus/usb/devices/` (`supervisor/src/supervisor/usb_devices.py`,
`GET /usb`), and the debug console already renders an SDR-flavoured verdict over that list
(`backend/src/jbrain/api/debug.py`).

This is the exact analogue of the SDR plan's **S0a** wave — the probe that shipped first
and told us the dongle was present but held by the DVB driver. Do the same thing here
before anything else: teach the same inventory a `KNOWN_BT_IDS` table and report
*found / found-but-no-driver-bound / absent*, then ask the owner to hit it from the PWA.

Note the second-order check: presence of the USB id is not presence of a working stack.
`btusb` must be loaded on the host. That is fixable without a terminal (§4.3).

### 4.2 Bluetooth sockets don't cross a network namespace

This is the design constraint that shapes the whole service.

Linux Bluetooth (`AF_BLUETOOTH`, HCI and RFCOMM sockets) works **only in the initial
network namespace** — per-netns Bluetooth was tried in the kernel and reverted. A
container on a Docker bridge network therefore cannot see `hci0` at all, no matter what
devices are passed in. There is no `devices:` line that fixes this, because RFCOMM is a
socket family, not a `/dev` node.

Consequences, all of which have to be accepted up front:

- The sidecar needs **`network_mode: host`** plus `cap_add: [NET_ADMIN, NET_RAW]`.
- `network_mode: host` is **mutually exclusive with `networks:`** in compose, so the
  sidecar **cannot join `internal`**, and the api cannot reach it by service name. It has
  to bind a host port and be reached by address.
- The `radio` / `render` trick — `internal: true`, so the kernel itself guarantees the
  container has no route off the box — is **unavailable**. A Bluetooth sidecar in the host
  netns has the box's full network reach by construction.

So isolation must be bought differently: **bind the sidecar's HTTP port to the Docker
bridge gateway address** (so containers can reach it and the LAN cannot) rather than
`0.0.0.0`, require the supervisor-style bearer token, and keep the container's job small
enough to audit — frame encoding and a serial write, no outbound HTTP client.

An alternative worth naming and rejecting: `rfcomm bind` a `/dev/rfcomm0` on the host and
pass *that* char device into an ordinary bridge-network container. It sidesteps the netns
problem entirely and it is the tidier picture — but the `rfcomm` binding utility is
deprecated in modern BlueZ, it needs the host stack anyway (§4.3), and a dropped link
leaves a stale node. Not worth the fragility.

### 4.3 Who owns the adapter — and the one thing the no-terminal path can't do

`deploy/update-inner.sh` is more capable than it looks. From the **PWA update path**, in a
privileged helper container, it can already:

- write host files (`host_file_write` — including a systemd unit or a modprobe drop-in) and
  restart the unit that reads them, via `nsenter` into PID 1's namespaces;
- write host kernel knobs (`host_kernel_write`);
- unload a host kernel module (`host_module_unload`) and unbind a driver from a device
  (`host_driver_unbind`).

What it **cannot** do is `apt`. The script says so in as many words at the earlyoom
thresholds: *"installing the package still needs apt (the host script's job)."*

That single limitation decides the architecture:

> **The sidecar ships its own BlueZ.** The container image installs `bluez` and runs its own
> `dbus-daemon` + `bluetoothd`, and owns `hci0` directly over the host netns. We never ask
> the owner to install a package on the host.

This works precisely *because* the host is a headless server image that almost certainly has
**no** `bluetoothd` running — so there is no contest for the adapter. (If it turns out one
*is* running, that becomes a genuine conflict and the design flips to "talk to the host's
BlueZ over its D-Bus socket" instead, mounting `/run/dbus`. Worth checking in D0.)

The one host-side prerequisite is kernel modules — `btusb`, and `rfcomm` for the socket
family. Those ship in the stock Ubuntu kernel; they just may not be loaded. That is a
`host_module_load` sibling to the existing `host_module_unload`, i.e. **~10 lines in the
update path we already own**, applied on the same no-terminal route the RTL-SDR blacklist
took. No new terminal dependency is introduced by this integration.

### 4.4 Pairing, with no terminal

The Ditoo pairs "Just Works" — no PIN, and the community work reports no authentication on
the SPP channel at all once connected. But *something* has to run the scan/pair/trust
dance, and `bluetoothctl` at a shell is not a thing the owner can do.

Driveable answer: the sidecar talks to its own `bluetoothd` over **BlueZ's D-Bus API** and
exposes the lifecycle as HTTP — `POST /scan`, `POST /pair {addr}`, `POST /forget`,
`GET /status` — which the api proxies to the debug console and a **Settings → Devices**
card. Pairing state persists in the container's `/var/lib/bluetooth`, so that must be a
named volume or the pairing is lost on every rebuild.

The status report should follow the SDR probe's discipline of distinguishing *absent* from
*present-but-not-usable*: **no radio / radio but nothing paired / paired but not connected
(likely the phone holds it) / connected**.

### 4.5 The ESP32 bridge — the variant that deletes §4.1 through §4.4

Put a **classic ESP32** next to the Ditoo, let *it* hold the Bluetooth link, and talk to it
from the box over Wi-Fi. Every hard part above is a consequence of the box owning the
Bluetooth radio; hand that job to a $8 board and they all evaporate:

| Hard part | With the ESP32 bridge |
|---|---|
| §4.1 Does the box have a BT radio? | **Moot** — the bridge has one. The D0 gate disappears. |
| §4.2 Bluetooth sockets can't cross a netns | **Gone** — the box speaks HTTP/MQTT over Wi-Fi. No `network_mode: host`, no lost `internal: true`. |
| §4.3 Who ships BlueZ / host modules | **Gone** — no BlueZ, no `btusb`, no `host_module_load`, nothing on the host. |
| §4.4 Pairing with no terminal | **Gone** — pairing lives in the bridge's NVS config, set from its web flasher. |
| ~10 m range (§6) | **Gone** — the bridge sits beside the speaker; only Wi-Fi has to reach the box. |
| The privileged sidecar itself | **Gone** — a pinned-URL client like `sdr_url`, or MQTT. |

**The display half is largely off the shelf.** `d03n3rfr1tz3/esp32-divoom` is exactly this
bridge: it lists **Ditoo** among supported devices, accepts commands over **Serial, TCP and
MQTT**, and ships a **browser-based web flasher** that writes config to NVS with no
toolchain — which is precisely the right shape for an owner with no terminal (rule 10). Note
its own constraint: *"Bluetooth Classic only exists on the classic ESP32"*, so this must be
an original ESP32 (`esp32dev`), **not** an S3, C3, C6 or H2 — those are BLE-only and cannot
speak SPP, A2DP or HFP at all, whatever a search result may tell you.

Better still, MQTT means it can ride the **Mosquitto broker this repo already ships** behind
the `mqtt` profile: the api publishes a frame to a topic and the bridge relays it. No new
container, no new transport.

**The audio half is net-new firmware.** esp32-divoom is display-only. ESP-IDF does expose
A2DP **source** and HFP **AG** on the classic ESP32, so it is possible — but the classic
ESP32 shares **one 2.4 GHz radio between Wi-Fi and Bluetooth**, and receiving an audio
stream over Wi-Fi while re-transmitting it over BT on that same radio is exactly where
coexistence bites. Treat throughput and dropouts as something to **measure on the bench**,
not assume.

**The mod that makes it good: put the microphone on the bridge, not the Ditoo.** An I2S MEMS
mic (INMP441, ~$3) soldered to the ESP32 beats the Ditoo's own mic on every axis:

- **wideband 16 kHz** instead of HFP's narrowband, straight into the shipped whisper;
- **no A2DP↔HFP switching**, so playback stays at music quality while listening (§5 limit 2
  disappears);
- **one less Bluetooth profile** to juggle on a tight radio — drop HFP AG entirely;
- **placement is yours**, unlike a mic buried in a speaker enclosure;
- and it gives server-side echo cancellation **a known reference signal** — the box knows
  exactly what it sent to the speaker — which is far more tractable than the blind AEC that
  §5 limit 1 otherwise forces.

The Ditoo then becomes a pure *output* device (pixels + speaker) and the bridge is the ears.

**What does NOT get better:** the Ditoo still accepts one Bluetooth link at a time, so the
owner's phone still steals it (§6); the protocol is still reverse-engineered; and custom
firmware is a new maintenance surface — an OTA that bricks the bridge needs physical access,
which is the one thing this owner does not have. The web flasher softens that, but it is
still a trip to the device.

**Do not open the Ditoo.** Tapping the panel internally means reverse-engineering an unknown
internal bus to replace a link that already works over the air, plus re-wiring the amp and
mic, irreversibly. The only internal mod worth considering is cosmetic: steal 5 V from the
USB-C input so the bridge hides inside the case and it stays one object.

## 5. Audio: in scope after all — and the earlier "no audio stack" objection was wrong

An earlier draft of this doc scoped audio out on the grounds that the box has no userspace
sound server (no `/dev/snd`, no PulseAudio, no PipeWire anywhere in `deploy/`), that A2DP
needs one, and that installing one is the apt gap of §4.3. **That reasoning does not hold**,
and the correction matters because it is what makes the Ditoo a complete device rather than
a display:

- **`bluealsad` (bluez-alsa) needs no sound server.** It talks to BlueZ directly and handles
  A2DP, HFP and HSP itself, exposing them as ALSA PCMs. It exists precisely so a system can
  do Bluetooth audio without PulseAudio or PipeWire, which is why it is the standard answer
  on headless and containerised boxes.
- **No physical sound card is involved**, so no `/dev/snd` and no host audio at all. Audio to
  a Bluetooth sink is encoded in userspace and written to the BT link; the box's own (absent)
  speakers never enter the path.
- Therefore it ships **inside the same sidecar that already ships BlueZ** (§4.3). Zero host
  packages, so rule 10 stays satisfied. The apt gap simply is not on this path.

**Capture works too, at a usable rate.** A Bluetooth speaker exposes its mic over HSP/HFP,
and `bluealsad` implements HFP natively (oFono optional). With **mSBC** that is **16 kHz
mono — exactly whisper's native input rate**, so the existing whisper.cpp in `tts-stt` can
consume it without resampling. The 8 kHz CVSD fallback would be poor; mSBC is the one to
negotiate. And the Ditoo does answer calls through its own mic, so HFP is present on the
device (reviewers describe call audio as muffled, which is the profile, not the hardware).

Four honest limits on the audio, none of them blocking:

1. **No hardware echo cancellation.** The Ditoo is a speaker with a mic, not a speakerphone
   with an AEC DSP, so the box hears its own TTS. The Wall gets this free from the browser
   (`getUserMedia({echoCancellation:true})` in `pet.html`); a raw ALSA path does not. Either
   gate the mic while speaking (half-duplex, simple, no barge-in) or run
   `webrtc-audio-processing`/`speexdsp` in the sidecar (full-duplex, real work).
2. **A2DP and HFP are exclusive.** Opening the mic drops the speaker to HFP's narrowband for
   the duration. Fine for an assistant turn; it means you cannot have hi-fi music and an open
   mic at once.
3. **One link at a time** (§6) — the room endpoint and the owner's phone are exclusive.
4. **~10 m of range** (§6).

So the sidecar of §7 D1 does all three jobs — display over SPP, playback over A2DP, capture
over HFP — from one container, on one Bluetooth link, with no host dependency beyond the
kernel modules of §4.3.

## 6. Constraints to accept before building

- **One link at a time.** Bluetooth Classic gives the Ditoo a single host connection. If
  the phone (or the Divoom app) is connected, the server is not. Community integrations
  report exactly this as the #1 support issue. Practically: the box owns the device, or
  the phone does — not both.
- **~10 m of range**, through whatever walls are in between. If the Ditoo lives somewhere
  else in the house, the honest fallback is a second piece of hardware — an ESP32 acting
  as a Wi-Fi→Bluetooth proxy (the `esp32-divoom` pattern) — which is a different project.
- **The protocol is unofficial.** It was recovered by decompiling the app and capturing
  HCI traffic. A firmware OTA can break it. Mitigation is unglamorous: don't take the OTA.
- **Input is unproven.** The community work is overwhelmingly *write*-only. Treat the
  keyboard, the buttons and the mic as not-available until a spike proves otherwise; do
  not design a feature that depends on the Ditoo sending anything back.
- **A screen in a room is a disclosure surface.** This cuts against non-negotiable #3 from
  an unusual direction: the device enforces nothing, so *we* must — whatever renderer feeds
  it must be firewalled the same way any other output is, and health, finance and location
  content must never be pushed to a panel sitting in a shared room. The pet, box vitals and
  a content-free "you have 3 notifications" are safe; the notification *body* is not.

## 7. Build sketch

Waves, in the repo's usual shape (`docs/reference/PROCESS.md`). D0 is a hard gate.

| Wave | What | Notes |
|---|---|---|
| **D0** | **Bluetooth presence probe.** `KNOWN_BT_IDS` in `supervisor/usb_devices.py`, a verdict in the debug console, owner runs it from the PWA. Also confirm no host `bluetoothd`. | Hours. Blocking — everything else is conditional on it. Mirrors SDR S0a. |
| **D1** | **The `ditoo` sidecar.** `deploy/ditoo/` + `Dockerfile.ditoo`, compose service under a `ditoo` profile (opt-in, never on a stock deploy), `network_mode: host`, `NET_ADMIN`/`NET_RAW`, own `bluez` + `dbus`, named volume for `/var/lib/bluetooth`. HTTP: `/status`, `/scan`, `/pair`, `/forget`, `/display`, `/text`, `/brightness`. Ships the frame encoder (`0xAA` framing, length, palette-indexed pixels at `log2(colours)` bits, checksum) over **RFCOMM channel 2** — audio-capable Divoom devices use 2, not 1. | The real work, and the real risk: does *this* unit's firmware speak the documented commands. Spike it against the physical device before committing to D2+. |
| **D2** | **Api seam.** `settings.ditoo_url` defaulting to `""` so the feature is simply absent when unset — exactly how `sdr_url` works — a pinned-URL client (**no tool ever supplies a host or URL**), debug endpoints, and a Settings → Devices card with scan/pair/test-pattern/brightness. | Conventional. Follows the SDR client precedent in `main.py`. |
| **D3** | **Renderers.** Clock, box-vitals face, notification card. 16×16 is a *design* problem — needs a `docs/mocks/` artboard per `docs/reference/DESIGN.md` before code. | |
| **D4** | **JPet on the panel.** One more `PetBroadcaster` subscriber + a 16×16 sprite renderer. | The payoff wave. |
| **D5** | **Workflow action** (`ditoo_display` in `ACTION_SPECS`) and, if wanted, one owner-scope agent tool with the §6 content fence. | |
| **D6** | **Voice out.** Add `bluealsad` to the D1 sidecar, A2DP playback, and a `/speak` endpoint the api feeds from the shipped Kokoro TTS. | No host audio (§5). Independent of D3-D5. |
| **D7** | **Voice in.** HFP capture negotiating **mSBC** (16 kHz — whisper's native rate), into the whisper.cpp already in `tts-stt`. Start **half-duplex** (mic gated while speaking); full-duplex barge-in needs `webrtc-audio-processing` in the sidecar and is its own wave. | The quality ceiling of the whole plan (§5). |

**Or, on the ESP32-bridge variant (§4.5) — the recommended shape.** D0, D1, D6 and D7 are all
replaced; D2-D5 survive unchanged because they sit above the transport:

| Wave | What | Notes |
|---|---|---|
| **E1** | Flash `esp32-divoom` to a **classic ESP32** from its web flasher, point it at the `mqtt`-profile broker, publish a test frame from the api. | Mostly config. No box-side C, no new container, no host changes. |
| **E2** | D2-D5 as written (client/settings, renderers, JPet, workflow action) against the MQTT topic instead of a sidecar URL. | Unchanged above the transport. |
| **E3** | **Audio-out spike.** A2DP source on the bridge, fed Kokoro audio over Wi-Fi. **Measure Wi-Fi/BT coexistence before committing** (§4.5). | The one place the hardware gets a vote. |
| **E4** | **Ears on the bridge.** I2S MEMS mic → Wi-Fi → the shipped whisper, with server-side AEC using the TTS as reference. | Beats the Ditoo's HFP mic on every axis (§4.5). |

Explicitly out of scope: keyboard/button input (§6 — the community work is write-only, so
treat device→box input as unproven).

New tables: **none** — the panel is a display, and its state is transient. If a "what's on
the panel" history is ever wanted, that is a new owner-only table and an RLS isolation test
(non-negotiable #3). `scripts/dev-setup.sh` and `deploy/install.sh` are touched in D1's PR
(non-negotiable #8) for the module-load step.

## 8. Recommendation

**Run D0 now — it is a few hours of work against code that already ships, and it converts
the whole question from speculation to a yes/no.** If the box has a Bluetooth radio, do D1
as a *spike* against the physical device before committing to the rest: everything
downstream is ordinary work on well-trodden seams, but D1 is the only part where the
hardware gets a vote.

**Keep the device** if the owner wants one small object that does pixel art, sound and
listening (§0.2) — nothing else on the market does, and the §4 cost is paid once for all
three. **Prefer the ESP32 bridge (§4.5)** over teaching the box Bluetooth: it deletes §4.1
through §4.4 outright, the display half is a browser-flashed off-the-shelf firmware, and it
rides the MQTT broker already in the compose. The trade is an embedded-firmware maintenance
surface, and an audio path that must be measured for Wi-Fi/BT coexistence before it is
trusted. Reach for a Wi-Fi panel only if the pixels alone matter (§0.1), and for a DIY ESP32
build only if audio quality outranks having a finished object.

If the box has no radio, the decision is the owner's: a USB Bluetooth dongle solves
presence (though not §4.2 — the netns constraint is unchanged), and the Ditoo remains a
perfectly good desk speaker in the meantime.

## 9. References

- `docs/plans/SDR_RADIO_PLAN.md` — the template for a hardware integration here.
- `supervisor/src/supervisor/usb_devices.py` — the free presence probe.
- `deploy/update-inner.sh` — `host_file_write` / `host_module_unload` / `host_driver_unbind`,
  the no-terminal host-mutation path, and its apt limitation.
- `backend/src/jbrain/jpet/broadcast.py`, `deploy/wall/` — the surfaces a panel would join.
- Protocol prior art (all reverse-engineered, none official): `d03n3rfr1tz3/hass-divoom`
  (Python, RFCOMM, lists `ditoo` among supported devices, documents the per-device channel
  and the "phone must not be connected" failure mode); `andreas-mausch/divoom-ditoo-pro-controller`
  and its write-up (frame format, palette encoding, how the commands were recovered);
  `d03n3rfr1tz3/esp32-divoom` (the Wi-Fi→Bluetooth proxy fallback). `r12f/divoom` is
  **Pixoo-64/REST** and does *not* apply to this device.
