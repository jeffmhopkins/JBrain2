# Divoom Ditoo on the box — feasibility + build sketch (proposed)

> **Status:** Proposed (icebox) · **Last verified:** 2026-09-12

**Status: proposed / icebox.** Nothing built, no roadmap slot. Written to answer one
owner question — *"I've ordered a Divoom Ditoo; can we integrate it on the server, and
how?"* — with a real answer rather than a shrug. When picked up, reconcile with the
`CLAUDE.md` non-negotiables, get a `docs/ROADMAP.md` slot, and promote out of
`proposed/` (per `docs/proposed/README.md`).

## 0. The verdict

**Yes — the 16×16 display is genuinely integrable, and it fits the shape this box already
uses for hardware.** It is *not* a drop-in like the SDR was, and the audio half of the
device is a much bigger project than the pixel half. Three things gate it:

| Gate | Status | How we find out |
|---|---|---|
| The box has a **Bluetooth Classic radio** | **Unknown — must probe** | Free: the shipped `GET /usb` supervisor probe already lists it (§4.1) |
| The speaker sits within **~10 m** of the box | Owner knows | Physical placement |
| Owner accepts an **unofficial, reverse-engineered** control protocol and that the **phone app must stay off it** | Owner decision | §6 |

If the first gate fails, everything below is dead until a ~$10 USB Bluetooth dongle is
plugged in — which is fine, but it is a physical act, and the owner runs this box
remotely (`CLAUDE.md` #10).

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

**Audio is deliberately *not* on this list.** See §5.

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

## 5. Audio: out of scope, and why

It is tempting — the box has a TTS service (`tts-stt`, Kokoro read-aloud) and this is a
15 W speaker, so "give the box a voice in the room" writes itself. Don't, not in v1:

- **The box has no audio stack whatsoever.** There is no `/dev/snd`, no PulseAudio, no
  PipeWire, no ALSA anywhere in `deploy/docker-compose.yml` or the deploy scripts. Today's
  read-aloud is *browser-side* — the Wall kiosk fetches audio and the browser plays it.
- A2DP to a Bluetooth sink means a real userspace sound server on the host (PipeWire, or
  `bluez-alsa`), i.e. host packages, i.e. the apt gap of §4.3 — the one thing the
  no-terminal path cannot close.
- There is no wired shortcut on this model: the base Ditoo's USB-C is **power only**
  (USB-C audio is a Ditoo *Pro* feature, and even there it is audio, not pixels — §1.1).

The pixel panel needs none of that: SPP is a serial socket, and BlueZ alone is enough.
Revisit audio only if the owner actually wants the box to speak aloud in that room, and
treat it as its own plan with a host-setup wave.

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

Explicitly out of scope: A2DP audio (§5), keyboard/button input, the mic.

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
