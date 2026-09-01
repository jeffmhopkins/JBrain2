# SDR radio — spectrum launcher, agent tools, and a transcribed recordings library

> **Status:** In progress · **Last verified:** 2026-09-01 · **Waves:** S0a✅ S0b-i✅ S0b-ii◻️(built, on-box gate pending) S1◻️ S2◻️ S3◻️ S4◻️ (S0a — the debug USB probe — shipped and **validated on the box**: it found a Nooelec NESDR SMArt v5, `0bda:2838`, serial `09022796`, held by `dvb_usb_rtl28xxu`, exactly the found-but-not-ready case it was built to distinguish. S0b-i — the DVB blacklist through the no-terminal update path — shipped on-branch. S0b-ii is the sidecar + client, then the on-box gate.)

> Reconciled with the root `CLAUDE.md` non-negotiables: transcription runs through the
> existing whisper client (rule 1 governs *completions*; speech-to-text already sits
> outside the adapter, as `jbrain/transcribe.py` records); recorded audio is written
> through the storage abstraction (rule 2); the one new table is owner-only with an RLS
> isolation test (rule 3); the sidecar is reached only through a pinned-URL client on the
> api — **no tool ever supplies a host or URL** (the `stream.py` SSRF guard is not
> widened, §4.4); scanner transcripts enter the **external corpus**, never the note
> pipeline, so machine-garbled radio traffic can never become wiki truth (rule 7); the
> host-enablement steps land in `install.sh` and `scripts/dev-setup.sh` in the same PR
> (rule 8); and every operator control is PWA-reachable (rule 10) — the one genuine host
> step is named in §5 S0 and scripted, not left as a terminal instruction.

Add a **software-defined radio** to the box: a PWA **Radio launcher** for live spectrum
and listening, a small set of **agent tools** for the parts a model can actually reason
about, and a **recordings library** whose transcripts are searchable through the
existing hybrid search. Built on one USB dongle, on the existing substrate — the
deferred-job path, the whisper chunker, the `AudioTranscript` player, the external
corpus, and the storage abstraction are all reused rather than rebuilt.

---

## 1. Why

The box has ears for everything *except* the radio spectrum: it can read a web page,
watch a stream, OCR an image, and transcribe an attachment, but a hundred megahertz of
live local activity — air band, ham repeaters, weather, ISM telemetry — is invisible to
it. An RTL-SDR is a $35 sensor that closes that gap, and almost every piece needed to
turn radio audio into searchable text is already built and shipped.

The honest framing: this is **a new sensor feeding existing pipelines**, not a new
pipeline. The work is a sidecar, a device lease, a launcher screen, and five tools.

## 2. The hardware we are building against

**Nooelec NESDR SMArt v5** — RTL2832U + R820T2/R860, 0.5 PPM TCXO, SMA female,
**receive only**, one tuner, ~2.4 MHz usable instantaneous bandwidth (3.2 max), 8-bit
ADC. Native tuner range 24 MHz – 1.766 GHz; HF below 25 MHz needs direct sampling and a
suitable antenna (**out of scope**, §9).

Three consequences drive the whole design:

1. **Receive only.** No transmit path, no transmit licensing surface, no TX tools.
2. **One tuner.** Listening, sweeping, and recording are mutually exclusive. Every
   consumer contends for a single device, so arbitration is a first-class component
   (§4.2), not an afterthought.
3. **~2.4 MHz of instantaneous bandwidth.** A wide sweep is `rtl_power` retuning across
   the band in steps — seconds to minutes, not instant. Sweeps are therefore **deferred
   jobs**, exactly like `analyze_stream`'s `full` mode.

## 3. Owner decisions (2026-09-01)

| # | Decision | Rationale |
|---|---|---|
| D1 | **Launcher owns real-time; tools own asking.** | The precedent is Images: generation/editing lives in the launcher, `analyze_image` (the read) is a tool. A model has no ears and a tool call is request/response, so "listen live" is incoherent as a tool. |
| D2 | **v1 = launcher + tools + manual recordings. No auto-record.** | Get hands-on with real local traffic before committing to a daemon that owns the only dongle full-time. Transcription quality on narrowband voice is the open risk; find out cheaply. |
| D3 | **Transcripts are external-corpus sources, searchable.** | Reuses the `persist_analysis` pattern so "what did they say about Oak Street" works through normal hybrid search. Explicitly **never notes** — notes are the sole sources of truth, and noisy machine transcription must not reach the wiki. |
| D4 | **Phase 2 auto-record = squelch watch on a channel list.** | The classic scanner behaviour and the one that feeds the library steadily. Deferred out of v1 (D2); its lease implications are designed for now (§4.2) so Phase 2 is additive. |
| D5 | **Waterfall = quantized power bins over binary WebSocket, rendered to canvas.** | ~1024 bins × 10 fps × 1 byte ≈ 10 KB/s — trivial on LAN, phone-friendly, and zoom/palette stay client-side. Server-rendered PNG strips would be a video stream for no benefit. |
| D6 | **Audio = Opus over HTTP chunked, played by a plain `<audio>` element.** | ~2–3 s latency is irrelevant for scanner listening, and it is a fraction of the code of WebSocket-Opus-into-MediaSource. Reversible: the transport is behind one endpoint, so a low-latency path can replace it without touching the UI's model. |
| D7 | **A lease-gated tuner control on the omnibox, in addition to the launcher.** | The composer grows a radio icon left of the attach clip, shown *only* while a tool holds the lease; tapping it opens the tuned-station controls. **The icon is the lease** — present means this session holds the radio, absent means idle or held elsewhere — so the arbitration in §4.2 stops being invisible plumbing. Follows the shipped plan-pill precedent (a live-state-gated composer affordance that opens a `<Sheet>`). **Chosen 2026-09-01: the bottom-sheet variant** — `../mocks/sdr-tuner/a-tuner-sheet.html`, now the binding spec; the icon stays a pure opener. |

## 4. Architecture — where it slots

### 4.1 The `sdr` service

A new **profile-guarded** container (`profiles: [sdr]`), matching how `comfyui`,
`local-llm`, and `mqtt` stay off a stock deploy. It holds `librtlsdr` and the standard
tools (`rtl_fm`, `rtl_power`, `rtl_sdr`), owns the USB device
(`devices: - /dev/bus/usb:/dev/bus/usb`, the same passthrough shape the GPU services
already use), and exposes a small internal HTTP + WebSocket surface.

**It sits on an egress-free network**, like `htmlrender`: an SDR service needs no
internet whatsoever, so it is given none by topology rather than by policy.

### 4.2 The device lease — the load-bearing component

One dongle means one owner at a time. The lease lives **in the `sdr` service** (single
process, single device — no table, therefore no new RLS surface), with:

- a **state**: `idle` | `listening` | `sweeping` | `recording`;
- a **holder** (which surface took it) and an acquisition timestamp;
- a **priority order**: interactive launcher use preempts a sweep; nothing preempts
  interactive use;
- a **TTL**, so a launcher tab that dies without releasing does not park the radio
  forever.

Every consumer that cannot get the lease fails gracefully — the tools return a
recoverable "radio busy, held by X" and the launcher shows it as honest persistent
status (DESIGN.md principle 5). This is the same problem the jcode LLM proxy already
solves for model residency (serialize swaps, evict to budget); the pattern is
deliberately reused rather than rediscovered.

Designing the lease now is what makes Phase 2 additive: a squelch watch simply becomes a
low-priority holder that interactive use preempts.

### 4.3 Data model

One new table, `app.sdr_recordings` (a new migration — owner-only, RLS-scoped, with an
isolation test per rule 3): frequency, mode, gain, squelch, started/ended, duration,
the **blob key** of the audio (written through the storage abstraction, never a raw
path), and the transcript payload in the shape `AudioTranscript.tsx` already consumes.

Transcripts additionally persist as **external corpus sources with passages**, following
`external/corpus.py`'s `persist_analysis`, which enqueues embedding and lands them in
hybrid search (D3).

### 4.4 The trusted-source lane

`analyze_stream` deliberately restricts ffmpeg to network protocols and runs
`guard_public_host` on both the input and the resolved URL, so a crafted URL cannot turn
ffmpeg into a read primitive against the box's own services. **That guard is not
widened, and no `skip_guard` path is added for the SDR.**

Instead the SDR gets its own lane: the api holds a **pinned-URL client** for the `sdr`
service (config, not model input), and the tools take *frequency and mode parameters* —
never a URL or host. Frequencies are validated server-side against the tuner's real
range and a band-plan allowlist. This is a security path: **100% coverage** per rule 5.

## 5. Waves

### S0a — the debug USB probe ✅ *(shipped on-branch)*

Answer the first question before building anything that depends on the answer:
**is the dongle there, what exactly is it called, and is anything holding it?**

Deliberately the cheapest possible spike. Enumerating and *naming* a USB device is
a **sysfs read** — `/sys/bus/usb/devices/` is not namespaced and Docker mounts the
host's `/sys` read-only into every container — so this needs **no device
passthrough, no privileges, and no `sdr` container**. Only *using* a device needs
`/dev/bus/usb`.

- `supervisor/src/supervisor/usb_devices.py` reads the bus, folds each device's
  interface drivers back onto it, and flags the RTL2832U family by USB id. The
  scan carries `sysfs_readable` alongside the device list, because apart they lie:
  an empty list means "no devices" only if we could look.
- Supervisor `GET /usb`, mirroring `/metrics` — the supervisor is already the only
  container that reads `/sys`.
- Api `GET /api/debug/sdr` proxies it and returns a **verdict**: `found`, `ready`,
  a one-line `summary`, and a `next_step`. The distinction that matters is
  found-but-not-ready: a dongle claimed by the kernel's DVB-T driver
  (`dvb_usb_rtl28xxu`) is the expected first result on a stock Ubuntu box, and the
  verdict says so and names the blacklist rather than leaving the reader to know it.
- An `sdr` command in the debug console, so it is one dropdown pick — **no terminal**
  (CLAUDE.md rule 10).

### S0b-i — free the device from the kernel's DVB driver ✅ *(shipped on-branch)*

S0a's on-box run returned exactly the case the verdict was built for:

```
found: true, ready: false   NESDR SMArt v5 (0bda:2838), serial 09022796
node /dev/bus/usb/001/005   drivers ["dvb_usb_rtl28xxu"]
```

The RTL2832U in an RTL-SDR **is** a DVB-T tuner chip, so the kernel's television
driver binds it on sight; two drivers cannot own one USB interface, and librtlsdr's
`libusb_claim_interface()` then fails with `-6`. The fix is a modprobe blacklist —
**and evicting the module already bound**, since a drop-in only stops the *next*
autoload.

Both are host operations, and the owner has no terminal (rule 10). Neither needs one:

- `host_file_write` in `update-inner.sh` already writes host files from the
  PWA-driven path (a `--privileged` one-shot with the target directory bind-mounted).
  Its own docstring cites rule 10 — *"that is how earlyoom's thresholds sat
  unapplied."* The blacklist drop-in rides it.
- A sibling `host_module_unload` reaches the host's modules through
  `nsenter -t 1`, the same way the existing helper reaches host systemd.

Applied **only when a dongle is actually claimed** (`sdr_dvb_claimed` looks for a
bound interface under the driver's sysfs directory), so a box with no radio, or one
already blacklisted, is untouched. Detecting the *condition* rather than a device-id
list also keeps it from drifting out of sync with the known-id table in
`supervisor/usb_devices.py`.

One correction this wave carries: the probe's success advice used to name the device
node. `devnum` increments on every re-plug, so it now leads with `/dev/bus/usb` plus
**selection by serial** — which the real dongle turns out to have from the factory,
retiring the plan's earlier note that a second radio would need `rtl_eeprom -s` first.

**On-box result, and the correction it forced.** The first run wrote the drop-in and
then failed to unload: `modprobe -r` **refuses an in-use module**, and a driver bound
to a device is in use. Worse, the fallback advice was wrong — a blacklist stops a
module being *loaded*, not one already resident, so re-inserting the dongle just lets
the loaded driver re-claim it and only a reboot would have helped. On a box the owner
runs remotely, "go unplug it" is not a fix they can perform at all.

The wave now **unbinds before unloading**: `host_driver_unbind` writes each bound
interface into the driver's sysfs `unbind`, dropping the refcount to zero so the
unload succeeds. sysfs is read-only in an ordinary container and read-write in a
privileged one — the same property `host_kernel_write` already relies on for
`/proc/sys`. The still-claimed message now says *reboot*, not *re-plug*.

### S0b-ii — the sidecar + client, then the gate

**Built.** The `sdr` image (`python:3.12-slim` + the `rtl-sdr` apt package; the service
is stdlib `http.server` piping to `rtl_fm`, so there are no new Python dependencies),
the profile-guarded compose service with the whole `/dev/bus/usb` tree passed through on
the egress-free `radio` network, a pinned `sdr_url` on the api, and
`POST /api/debug/sdr/capture` — tune, record, transcribe, in one console command.

The permission question resolved to **run as root**, deliberately: the alternative (a
udev rule assigning a group, then joining that GID) solves a problem this container does
not have. It is egress-free, holds no owner data, and its entire job is to open one USB
device. The GPU services join host GIDs because they must run non-root for other reasons.

**Enabled by the hardware, not by a flag.** The service is profile-gated, and the
update path turns the profile on when it finds a dongle on the bus (`sdr_present`),
writing `SDR_ENABLED`/`SDR_URL` once so `deploy/jbrain` activates the same profile over
SSH. Asking the owner to set a flag would have meant asking them to edit `.env` — an
instruction they cannot follow (rule 10). Plug the radio in, run Update, the service
appears. The one cost is a second copy of the USB id list in shell, which a test pins
against `KNOWN_SDR_IDS` so drift fails CI rather than silently disabling the radio.

Two design points carried from earlier waves: the sidecar holds a **lock** and refuses a
second caller with `409` rather than queueing (one tuner — an unknown wait is worse than
a plain no), and the capture reports **`peak`/`heard_something`** alongside the
transcript, because a dead antenna and a working capture of silence produce audio of
identical length and whisper will confabulate words over noise.

**This is the blocking gate.** S0a proved the device is visible and nameable and
S0b-i frees it; S0b-ii must still answer, on the real box with the real antenna: does
the dongle survive a stack restart and re-plug, selected by serial; what is actually
audible locally; and — the open risk — **is whisper's output on narrowband voice good
enough to be worth a library?** A negative answer reshapes S3/S4 rather than being
discovered after they are built.

### S1 — the lease + the control API

The lease state machine (§4.2) with its priority order and TTL. The control endpoints:
tune, set gain/squelch, start/stop listen, start/stop record, status. The waterfall
WebSocket (D5) and the audio stream endpoint (D6). Frequency/mode validation and the
band-plan allowlist, with the security tests at 100%.

### S2 — the recordings library

The `app.sdr_recordings` migration + RLS isolation test. Blob write through the storage
abstraction. Transcription via the existing `transcribe_audio_chunked` (already chunked,
already tolerant of a failed chunk). Corpus persistence + embedding enqueue per D3. The
query API behind the `sdr_recordings` tool and the launcher's Recordings tab.

### S3 — agent tools

The five tools of §6, as `.tool` sidecars + handlers, registered through
`toolregistry.py`. `spectrum_sweep` runs as a **deferred job** with a progress card,
reusing the `media_analysis_results` pattern rather than adding a second one.

### S4 — the Radio launcher

**S4a is the GUI gate** (PROCESS.md §GUI gate): three interactive mock HTML artifacts
presented to the owner to choose before any implementation. The chosen mock lands in
`docs/mocks/` and becomes binding spec. The architectural decisions in §3 (D1, D5, D6)
and the tab structure are settled; what the mocks explore is layout, the tuning
interaction, and how the lease state is surfaced. **The omnibox-tuner half of this
gate is CLOSED** — three mocks ran in `../mocks/sdr-tuner/` and the owner chose the
bottom sheet (`a-tuner-sheet.html`, now carrying a BINDING SPEC header with the
component contract). The **Radio launcher still needs its own mock round** before
S4b touches it.

**S4b** implements two surfaces. The **Radio launcher**: tabs mirroring the Math
launcher's pattern — **Spectrum** (waterfall + tuning + listen controls) and
**Recordings** (the library, reusing `AudioTranscript.tsx` as-is for playback and
word-level transcript). And the **omnibox tuner** (D7): a new glyph in
`components/icons.tsx`, an `sdrActive`/`onSdrTap` prop pair on `Omnibox` rendered
inside `.foot-icons` exactly as the attach button is, fed from `HomeScreen` — the
`attachEnabled` line is the template for a capability-gated foot icon, and
`planStatus`/`onPlanPillTap` for a tappable live-state-gated one. The "tuning is
active" flag rides a module pub/sub store shaped like `hostVitals.ts`'s
`subscribeModelLoad`/`useModelLoad`; there is no React context in this frontend.
Phone-first, bottom-half controls, ≥44px targets, tokens only.

The launcher and the omnibox tuner divide by depth, not duplication: the tuner is
the quick control while chatting; the launcher owns what needs screen area — the
waterfall and the recordings library.

## 6. Interfaces

### Agent tools

| Tool | Permission | Cost | What it does |
|---|---|---|---|
| `sdr_status` | `read` | cheap | What the radio is doing, who holds the lease, current tuning |
| `sdr_listen` | `external` | expensive | Tune + capture N seconds + transcribe → transcript card |
| `spectrum_sweep` | `external` | expensive | `rtl_power` across a band → chart card + detected-activity list. Deferred job |
| `sdr_recordings` | `read` | cheap | Query the library by frequency, time, or transcript text |
| `sdr_watch` / `sdr_unwatch` | `mutate` | cheap | Arm/disarm auto-record (**registered in Phase 2**, specified here so the lease design accounts for it) |

A sweep returns numbers; numbers alone are not interpretable. `spectrum_sweep` therefore
joins detected activity against a **band-plan table** so the model can say "462.5625 —
FRS channel 1" rather than reciting frequencies. The band plan is reference data, not
model input.

### Control API (owner-only, launcher-facing)

`GET /api/sdr/status` · `POST /api/sdr/tune` · `POST /api/sdr/listen` (start/stop) ·
`POST /api/sdr/record` (start/stop) · `GET /api/sdr/audio` (chunked Opus, D6) ·
`WS /api/sdr/waterfall` (binary power bins, D5) · `GET /api/sdr/recordings`.

## 7. Open decisions (deferred, not dropped)

- **Whisper vocabulary biasing.** Local callsigns, agency names, and street names as an
  initial prompt measurably help on comms audio. Deferred until S0 shows the baseline.
- **FM bandstop filter.** The 8-bit ADC's limited dynamic range means a strong local
  broadcast station can desense the tuner. A hardware answer (~$15), flagged here so a
  disappointing S0 is diagnosed rather than blamed on software.
- **Waterfall history depth** — how much scrollback the client keeps.
- **CPU whisper in the `sdr` container.** Not needed in v1 (transcription is on-demand,
  so the gateway's whisper is fine). Phase 2's continuous watch would pin whisper
  resident and contend with the chat models for the iGPU; a small CPU-only whisper in the
  sidecar is the likely answer. Decided when Phase 2 is scheduled.

## 8. Docs to reconcile on merge

- `docs/reference/SERVICES.md` — the supervisor's `/usb` command (done, S0a), then the
  `sdr` service row, the profile, the Radio launcher in the owner-app screen list, and
  the new tools in the tool inventory.
- `docs/runbooks/DEBUG_ACCESS.md` — the `sdr` console command (done, S0a).
- `docs/reference/DESIGN.md` — only if the waterfall needs a token or compact-density
  variant that does not already exist.
- `docs/ROADMAP.md` — a slot (done, at promotion out of `proposed/`).
- `docs/mocks/sdr-tuner/` — the chosen tuner mock, as binding spec (its README's
  Decision section filled in), plus the launcher's mock round when it runs.
- This plan — promoted from `docs/proposed/` to `docs/plans/` when scheduled, then
  archived when all waves land.

## 9. Out of scope (named, not silently dropped)

- **Transmit.** The hardware cannot, and the tools will not pretend otherwise.
- **HF below 25 MHz.** Direct sampling plus an HF antenna; the interesting voice traffic
  is above it.
- **Trunked / P25 systems.** Needs OP25 or trunk-recorder — a substantially larger lift.
  Worth knowing that much local public-safety traffic is now trunked and often
  encrypted, so S0 may find little clear voice depending on the county. Encrypted
  traffic is not decodable and no attempt is made.
- **ISM decoding (`rtl_433`), ADS-B (`dump1090`), weather-satellite imaging.** All are
  natural later additions on the same sidecar — each is a decoder plus a table, and the
  lease already arbitrates them. None are in v1.
- **A second dongle.** The single-tuner constraint is designed around, not engineered
  away. A second radio is the cheapest fix for Phase 2's lease contention and is noted
  as the unlock, not assumed. If it happens, dongles must be given unique serials with
  `rtl_eeprom -s` and selected by serial, since identical dongles enumerate in
  non-deterministic order.
- **Scanner transcripts as notes.** Deliberate and permanent (D3).
