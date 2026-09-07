# SDR radio — spectrum launcher, agent tools, and a transcribed recordings library

> **Status:** In progress · **Last verified:** 2026-09-07 · **Waves:** S0a✅ S0b-i✅ S0b-ii◻️(built, on-box gate pending) S1◻️ S2◻️ S3◻️ S4◻️ (S0a — the debug USB probe — shipped and **validated on the box**: it found a Nooelec NESDR SMArt v5, `0bda:2838`, serial `09022796`, held by `dvb_usb_rtl28xxu`, exactly the found-but-not-ready case it was built to distinguish. S0b-i — the DVB blacklist through the no-terminal update path — shipped on-branch. S0b-ii is the sidecar + client, then the on-box gate.)

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
ADC. Native tuner range 24 MHz – 1.766 GHz, and **HF below that is reached by bypassing
the tuner** (shipped 2026-09-04) — the unit is **sold as 100 kHz–1.75 GHz**,
because the RTL2832U's ADC can be fed directly, bypassing the R820T2, which is how any
RTL-SDR reaches HF. So HF is a software gap here rather than a missing part: nothing in
`deploy/sdr/` passes a direct-sampling flag, and `MIN_HZ`/`MIN_MHZ` block the range
regardless (**out of scope for now**, §9 — but for reach, not for hardware).

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
| D6 | **Audio = MP3 over HTTP chunked, played by a plain `<audio>` element.** | ~2–3 s latency is irrelevant for scanner listening, and it is a fraction of the code of WebSocket-into-MediaSource. Reversible: the transport is behind one endpoint, so a low-latency path can replace it without touching the UI's model. **Amended 2026-09-01 after the first on-box listen:** this shipped as Opus-in-WebM and did not work, because a container with an initialisation header only decodes for a listener who received that header. Listeners here join late and repeatedly — one lease outlives many openings of the sheet — so late joiners got a headerless stream, and a retune spliced a second header into connections already in flight. MP3 is self-framing: any byte offset is a valid place to start. **Amended again after the second listen:** the `<audio>` element is parked hidden in `<body>` for the life of the lease and never moved, because the HTML spec pauses a media element the moment it is removed from a document — lending it to the sheet and taking it back silenced the radio on every close, and jsdom does not implement that step, so the unit test agreed with the bug. The transport is play/pause only; a live stream has no timeline for the native controls to scrub. Both verified against real Chromium, which is now the only thing trusted about media behaviour here. |
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

**Built.** The `sdr` image (then `python:3.12-slim` + the `rtl-sdr` apt package, the
service stdlib `http.server` piping to `rtl_fm` — `SDR_IQ_SPECTRUM_PLAN.md` F1 has since
rebased it onto `debian:trixie-slim` and taken numpy + SoapySDR from apt, so the image's
rule reads apt-only-no-pip rather than stdlib-only),
the profile-guarded compose service with the whole `/dev/bus/usb` tree passed through on
the egress-free `radio` network, a pinned `sdr_url` on the api, and
`POST /api/debug/sdr/capture` — tune, record, transcribe, in one console command.

The permission question resolved to **run as root**, deliberately: the alternative (a
udev rule assigning a group, then joining that GID) solves a problem this container does
not have. It is egress-free, holds no owner data, and its entire job is to open one USB
device. The GPU services join host GIDs because they must run non-root for other reasons.

**On-box result.** The profile auto-enabled, the image built, and the container came up
healthy on the first try — but the first capture answered *"No SDR on this box"*. The
updater wrote `SDR_URL` into `.env` and nothing carried it into the api: compose maps
`.env` keys onto `JBRAIN_*` container env, and that one line was missing, so a healthy
sidecar sat unreachable behind an unset setting. The mapping defaults **empty** rather
than to `http://sdr:8000`, because a hardcoded default would turn "this box has no
radio" into a DNS failure instead of the clean 503 that says so.

**Enabled by the hardware, not by a flag.** The service is profile-gated, and the
update path turns the profile on when it finds a dongle on the bus (`sdr_present`),
writing `SDR_ENABLED`/`SDR_URL` once so `deploy/jbrain` activates the same profile over
SSH. Asking the owner to set a flag would have meant asking them to edit `.env` — an
instruction they cannot follow (rule 10). Plug the radio in, run Update, the service
appears. The one cost is a second copy of the USB id list in shell, which a test pins
against `KNOWN_SDR_IDS` so drift fails CI rather than silently disabling the radio.

Two design points carried from earlier waves: the sidecar refuses a second caller with
`409` rather than queueing (an unknown wait on a radio someone else is using is worse
than a plain no) — **per radio** since `APRS_CONTROL_PLAN.md` P0b, so the refusal names
the dongle and a second one is genuinely a second tuner; and the capture reports **`peak`/`heard_something`** alongside the
transcript, because a dead antenna and a working capture of silence produce audio of
identical length and whisper will confabulate words over noise.

**This is the blocking gate.** S0a proved the device is visible and nameable and
S0b-i frees it; S0b-ii must still answer, on the real box with the real antenna: does
the dongle survive a stack restart and re-plug, selected by serial; what is actually
audible locally; and — the open risk — **is whisper's output on narrowband voice good
enough to be worth a library?** A negative answer reshapes S3/S4 rather than being
discovered after they are built.

**S0b-ii's gate is closed, and it verified itself.** Tuning 99.3 MHz wide FM on the
box returned `peak 0.432` and this transcript:

> *"of the week on Instagram at LightRock993 and LightRock993.com every Wednesday.
> The pet of the week brought to you by Seacoast Air Conditioning."*

**LightRock993** is WLRQ-FM's own on-air branding at exactly the frequency requested,
and Seacoast Air Conditioning is a Brevard County advertiser. There is no way to
produce that text except by receiving that transmitter — a self-verifying result
rather than a plausible one. Every link is now proven on hardware: enumeration,
the DVB unbind, the container claiming the device, demodulation at the right
frequency, and transcription.

What it does **not** answer is the question the plan turns on. Broadcast FM is clean,
wideband, professionally produced audio — near whisper's best case. Narrowband voice
comms are 3 kHz, compressed, clipped and bursty. A good result here was necessary and
is nowhere near sufficient; it proves the *pipeline* has no defects of its own, so if
narrowband comes back as mush that will be the audio rather than the plumbing.
Deliberately deferred — the owner chose to finish the tuner surface first.

**A gap the first tuner deploy exposed.** Shipping the lease *viewer* without a lease
*taker* made the surface unreachable: the icon appears only while a session holds the
tuner, sessions start only through an `OwnerDep` route with no UI behind it, and the
tool did not exist yet. "The icon is the lease" necessarily implies something else
takes the lease first, and nothing did. `sdr_listen`/`sdr_stop` close it, and debug
twins of listen/stop make the radio drivable from a handed-over token — the owner
surface is owner-cookie-gated by design, so a capability token cannot reach it.

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
component contract). **The launcher's round is CLOSED (2026-09-04): shape A**, the radio as the object —
`../mocks/sdr-launcher/shapes.html`, now the binding spec, with its README recording
that it supersedes round 3's APRS-switch placement and why.

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

### S4c — the live waterfall ✅ *(shipped on-branch 2026-09-04)*

**A waterfall is a radio held open, not a measurement that ends.** So it is a lease
purpose of its own — `spectrum` — rather than a flag on `survey`: the same `rtl_power`
with no exit timer and its CSV on stdout, read line by line and fanned out to viewers as
rows are measured. A survey ends by itself and is then reduced; this keeps being drawn,
and the two are released differently and named differently in a refusal.

**1 fps, at any width** (the owner's choice, 2026-09-04). `bands.py` already carried a
`fast` tier — one hop, `rtl_sdr` + FFT, ~10 fps, the radio never looking away — and it
is deliberately NOT built yet: the streamed-`rtl_power` tier serves every span including
the narrow ones, and building the fast path first would have shipped a picture that
worked only on bands narrow enough to fit one hop.

What the picture says about itself is load-bearing rather than decoration: past one hop
`rtl_power` retunes within each interval, so any given frequency is watched for a
fraction of that second and a burst can fall between visits. The tab prints the section's
`duty` from the server, because a waterfall that hid that would look identical to one
that could not miss anything.

**CONFIRMED ON THE BOX 2026-09-04**, on the general dongle while APRS kept logging on
the dedicated one. What the measurement settled, in order of how much it was a guess:

- **rtl_power streams.** Rows reached the PWA within a second or two of starting, so the
  `stdbuf -oL` insurance was never load-bearing — but it stays, because what was tested
  is a binary apt installed, not one this repo builds.
- **The stitching model is the tool's own.** 8 blocks per interval across 88-108 MHz, all
  carrying one timestamp, constant width for 60 intervals — which is exactly the shape
  `Stitch` assumes, including that the learned width lets every frame after the first
  land on its last block rather than on the next interval.
- **The colour scale lands where it has to.** Calibrating over the first 8 frames of the
  real numbers gives low −7.2 dB / high +14.9 dB; the noise floor (p50 −4.7) sits at
  t=0.11 — dark blue — and the four strongest stations at t=0.69-0.87 — steel through
  amber, with headroom above. No adjustment was needed, which is the outcome the
  percentiles were borrowed from `waterfall_png` to get.
- **What the radio grants is not what it was asked for.** A request for 88.000-108.000 at
  25 kHz came back as **1032 bins of 19531 Hz covering 88.000-108.156** — the largest
  power-of-two division of the per-hop bandwidth, and blocks that tile past the top edge.
  The band button printed the REQUEST directly above a picture whose axis printed the
  grant; it now reports the grant as soon as a row has arrived. Two numbers for one
  measured fact, and the more prominent one was the wrong one.

**The picture runs bottom-up** (owner's call, 2026-09-04): newest row against the
frequency axis it is measured on, history rising away from it. Built the other way
first — SDR# and gqrx both default to newest-at-top — so this is a preference between
two real conventions rather than a correction. It is indexed from the bottom rather
than drawn and flipped, because a half-full waterfall flipped whole would put its blank
half over the newest rows.

Three decisions worth keeping:

- **The colour scale is calibrated once, then held.** Re-taking it per row renormalises
  the picture around whatever is on the air, so a carrier appearing darkens the noise
  floor and a band going quiet blooms — exactly the two changes the owner is watching
  for, erased by the act of watching. The percentiles and the ramp are `waterfall_png`'s,
  so a still image of a sweep and the live picture of the same band are the same picture.
  (This is why no new DESIGN.md colour token was needed, which the mock round had left
  open.)
- **A row is drawn when its audio is heard, whenever there is audio to match it.** This
  read the other way round until the day it named arrived: a spectrum session is its own
  purpose on its own radio and makes no sound, so there was nothing to align with, and a
  delay added in advance would only have made the picture late. One radio now both
  demodulates and draws, off the same samples, so every row of a LISTENING session has
  an ear. A spectrum-only session still draws straight through, and so does a paused
  radio.

  **Order matters, and the first pass got it wrong (2026-09-07).** The hold was built
  while playback was still ~8 s behind, which made a correct mechanism look like an
  eight-second penalty — the owner's answer was "I'd rather have more real time with the
  spectrum and the audio versus the closed captioning being in sync", and the hold was
  thrown away. It is back, in the right order: **cut the audio latency first, then close
  what is left exactly.** `Frame.at` and the audio anchor are the same box clock, so the
  closing is exact rather than approximate. The owner's ranking is on the record — "is
  there a way to perfectly sync the spectrum to the audio? that's the most important
  thing to actually be synced" — and the thing this delays is the PICTURE, never the
  sound.
- **Shortwave listens and cannot be drawn.** `rtl_power` hardcodes direct-sampling mode
  1 — the ADC's I branch — while this hardware wires Q, so the band picker disables those
  rows with the reason on them rather than offering a tap that ends in a 400.

### S4e — a radio that will not open ✅ *(shipped on-branch 2026-09-04)*

**Found by a sweep that answered `complete: true` with zero rows.** One dongle's USB
descriptors had stopped answering, so librtlsdr enumerated it with blank strings and
every `-d <serial>` lookup failed:

    Found 2 device(s):
      0:  , , SN:
      1:  Nooelec, NESDR SMArt v5, SN: 09022796
    No matching devices found.

`rtl_power` printed that and exited in milliseconds. Three separate defects turned a
hardware fault into something only a container-log read could explain — which is exactly
what the owner cannot do (CLAUDE.md #10):

- **`start` answered 200 for a pipeline that could not work.** The api reported a
  session, the omnibox lit, and a moment later the tuner reaped a dead one. Sessions are
  now watched for `STARTUP_GRACE_S` and REFUSED if the radio did not open, carrying the
  line that says so. 0.4 s on every start: the price of a 200 meaning "the radio opened"
  rather than "a process was spawned".

  **It refuses on the tool's WORDS, not on the process dying**, and that distinction is
  the box's correction rather than a design instinct. The first cut waited for an exit,
  on the stated grounds that these tools "fail in milliseconds" — and the re-test after
  deploying it showed `rtl_power` printing `No matching devices found` and then carrying
  on with its hop plan, alive well past the grace. Waiting for a death catches only the
  fast half; the announcement is always there, and earlier. `_CANNOT_OPEN` matches it,
  and the liveness check stays as the general case for a pipeline that dies without an
  explanation that list knows.
- **A sweep that measured nothing reported success.** `complete` meant "did not overrun",
  so an empty CSV read as a finished sweep of a quiet band. An empty result is now a 502
  carrying the same words. This is the check that actually caught the fault on the
  re-test, the startup one having been fooled by a tool that complained and kept
  running — which is the argument for having both.
- **The sidecar's 400s arrived as 502s.** "a sweep cannot go below 24 MHz" and "the radio
  did not start" are sentences for an operator, and wrapping them in `sdr sidecar: …`
  buried the actionable half behind a status that reads as the box being broken.

**And a dongle that has stopped answering can now be reset REMOTELY.** That is the part
that mattered most on the day: the fault appeared while the owner was away, and the only
recovery anyone could name was walking to the box and re-plugging it. `USBDEVFS_RESET`
on the device node is a port reset — the kernel re-enumerates exactly as a re-plug does —
and the sdr container already has `/dev/bus/usb` and runs as root, so it needs no new
privilege and no new package (`deploy/sdr/usb.py`, stdlib `fcntl`).

What makes it possible for the broken case specifically is that **sysfs still names the
device**: the kernel answers from what it cached at enumeration, so a serial still maps
to a node long after librtlsdr has stopped being able to identify it. So the api resolves
serial → node from the supervisor's scan and the sidecar resets that node — never a path
from the caller, and never a device that is not an SDR. It runs through the lease, so a
reset cannot happen under a decode, and it touches one device, so APRS on the other
dongle keeps logging. Reachable from the PWA (**Radios → the radio → Reset this radio**)
and from a handed-over token (`scripts/debug-connect.sh sdr-reset`). CLAUDE.md #10 asks
for terminal dependencies to be designed out rather than documented; this is one.

**RUN ON THE BOX 2026-09-04, and it did not go well.** The reset was issued against
`77192819` and the dongle **left the bus and did not come back** — 13 USB devices before,
12 after, the serial absent from the scan entirely rather than present-and-unreadable.
Two things follow, and both are recorded because the second is the more useful:

- **It is evidence for the power theory, not the SIGKILL one.** A healthy device returns
  from `USBDEVFS_RESET` in moments; one that cannot complete enumeration afterwards is
  one that is not drawing what it needs, or is failing. Nothing was lost in capability —
  the radio was already unopenable — but the state changed from "present and useless" to
  "absent", and only a re-powered port (a re-plug or a reboot) brings that back.
- **Two defects surfaced with it**, both since fixed: the ioctl outran the api's ordinary
  30 s sidecar timeout, so an operation that HAD happened came back as a 500 with a
  traceback (`RESET_TIMEOUT_S`, and a 504 that says "look again" rather than "it
  failed" — the honest reading of a timeout, and the only one that is not a lie here);
  and `GET /api/debug/sdr` threw outright when the supervisor's `/usb` read timed out,
  which is precisely what a bus does while a device re-enumerates. It now answers
  "cannot tell", a state that shape already carried.

**And the cause may have been ours.** `_kill` sent SIGKILL with no SIGTERM first, and
both tools install a handler that cancels the pending async USB transfer and CLOSES the
device. SIGKILL never runs it, leaving the RTL2832U with transfers submitted — a known
way to leave one needing a reset before its descriptors read again. It is now SIGTERM,
two seconds, then SIGKILL only for a tool that will not go. Not proven to be what
happened (a powered hub is the owner's own theory, and power starvation fits the
symptom too), but the code was wrong either way.

### S4d — the Radios tab, shape A ✅ *(shipped on-branch 2026-09-04)*

**The radio is the object.** Tabs are `Radios | APRS | Recordings`; the first is a
roster of what each radio is doing, and tapping one opens a control layer where its job
— Listen / APRS / Spectrum / Idle — is chosen. The Tuner tab and the interim Spectrum
tab are both gone: each was a place where a job lived apart from the radio running it,
which is exactly the split shape A was chosen to remove.

**A tap on a radio is honoured or refused BY NAME.** `roles.named` is the new half of
`roles.py`: `choose` answers "which radio should do this", `named` answers "may that
one". Both are needed and neither is the other — quietly serving the job from a
different dongle is the same silent substitution the module already existed to prevent,
reached from the opposite direction. `/sdr/listen`, `/sdr/spectrum` and `/sdr/aprs` all
take an optional `serial`; omitting it keeps the old behaviour exactly.

Three rules fall out of it, each one a sentence the owner reads rather than a 409:

- **Dedication binds the tuner too.** A radio reserved for APRS is not one the waterfall
  may borrow because APRS happens to be idle, so its other jobs are disabled with the
  reason under the control — not only in a `title`, which a phone has no way to show.
- **Busy is left to the lease.** The sidecar holds one session per radio and answers
  with a 409 naming the job that has it, which is a better sentence than anything the
  api could compose from a serial list and the only one that cannot already be stale.
- **A named radio survives a scan that could not see.** There is then no way to check
  whether it is attached — but the owner named it, and passing it through beats the
  historical "whatever librtlsdr enumerates first": if it is gone the sidecar fails on a
  device it can prove is missing rather than opening the other antenna.

**Changing a radio's job frees it first**, and asks twice before doing so (DESIGN.md's
inline confirm). One of the things a job change stops is an APRS log the owner may have
armed on a schedule — the silent loss the sidecar's own `_stop` is written against.

**The APRS switch moved with it** (`APRS_CONTROL_PLAN.md`, where the supersession is
recorded): the APRS tab keeps its log, health line, roster and command tasks, and where
the switch was it now carries a pointer. One state, never two switches.

## 6. Interfaces

### Agent tools

| Tool | Permission | Cost | What it does |
|---|---|---|---|
| `sdr_status` | `read` | cheap | What the radio is doing, who holds the lease, current tuning |
| `sdr_listen` | `web` | expensive | Tune the radio and take the lease → puts the tuner icon in the composer (shipped) |
| `sdr_stop` | `web` | cheap | Release the radio → the icon disappears (shipped) |
| — | — | — | **Live captions** are not a tool: `GET /api/sdr/captions` (SSE) streams whisper transcription of the live audio to the tuner sheet's CC toggle (shipped) |
| `spectrum_sweep` | `external` | expensive | `rtl_power` across a band → chart card + detected-activity list. Deferred job |
| `sdr_recordings` | `read` | cheap | Query the library by frequency, time, or transcript text |
| `sdr_watch` / `sdr_unwatch` | `mutate` | cheap | Arm/disarm auto-record (**registered in Phase 2**, specified here so the lease design accounts for it) |

**Why the live tools are `web`, not `external`.** `external` means egress: the class
stages an owner Proposal because a payload is about to leave the box (invariant #9).
The tuner emits nothing — it is an RX-only receiver on an `internal: true` network —
so there is no payload to approve, and staging one would put a consent card in front
of "put on 99.3". `web` is the right class for the same reason `transcribe` and
`analyze_video` carry it: an on-box sidecar, opt-in per agent, no owner data in the
call. The class is also the *access* gate, and it cuts both ways: `web` is admitted
only to an agent that explicitly allowlists it, so jerv must name these in
`JERV_TOOLS` (it does) and the Full Brain curator's `tools=None` wildcard can never
absorb the radio. Shipping them as `external` got both halves wrong at once — jerv
could not see them, and curator could. Pinned by
`test_the_radio_tools_reach_jerv_and_only_jerv`.

A sweep returns numbers; numbers alone are not interpretable. `spectrum_sweep` therefore
joins detected activity against a **band-plan table** so the model can say "462.5625 —
FRS channel 1" rather than reciting frequencies. The band plan is reference data, not
model input.

**Calibrating the detector, measured on the box (2026-09-03).** `POST /api/debug/sdr/sweep`
exists to establish this box's numbers before `spectrum_sweep` is designed against them,
and the first real runs paid for themselves:

- A transient IS found: APRS turned up at 144.3906 MHz (peak +10.2 dB, occupancy 6.7% on
  the first run; -11.9 dB at 5% on a later gain-30 run). Occupancy works.
- A 342 kHz span (145.872-146.206) came back missing, straight across live repeater
  channels, with nothing in the response saying so. `reduce_csv` had trimmed each retune
  block to its SHORTEST row, so one truncated interval deleted that block's tail from the
  whole window. **Fixed and confirmed on hardware**: the same 144-148 sweep now returns
  1026 contiguous bins in two blocks (144.000-146.004, 146.000-148.004 — overlapping, not
  gapped) with `uncovered: []`. And `uncovered` now reports any span a sweep did not
  measure, because "nothing there" and "never looked" are not the same answer.
- `rtl_power` reports **relative dB, not dBm** (floor -5.3 with AGC, -27.4 at gain 30),
  and `bin_hz` is a hint: asking 5 kHz got 2734 Hz over 1.4 MHz and 3906 Hz over 4 MHz —
  the nearest power-of-2 FFT for the span, so it changes with the span.

**A claim this plan carried, now retired.** An earlier entry here said the two retune
halves measured "1.76x apart" and that six repeater outputs sat lit on the waterfall
while the table reported none. Both came from reading PIXEL BRIGHTNESS off the returned
PNG, and the waterfall palette is stretched between the 20th and 99.5th percentile of
the data — so a few dB of spread fills the whole palette and any ratio taken off it
means nothing in dB. Measured on the numbers: **the two blocks of one 144-148 sweep sit
0.68 dB apart**, and nothing was lit.

**What a quiet 2m band measures like on this box** (three sweeps, 2026-09-03 ~22:30 UTC,
120 s each, AGC and gain 30):

| | AGC 146-147.4 | gain 30 146-147.4 | gain 30 144-148 |
|---|---|---|---|
| floor median | -5.3 dB | -27.4 dB | -27.5 dB |
| total floor spread | 7.2 dB | 6.9 dB | 6.7 dB |
| excess over local floor, p50 / p90 / p99 | 0.0 / 0.5 / 2.0 | 0.0 / 0.4 / 0.8 | 0.0 / 0.3 / 1.4 |
| strongest bin | 146.6616, +4.8 | 146.6616, +3.5 | 147.4569, +2.9 |

Two things follow. **Fixed gain does not open the dynamic range** — it shifts the floor
down ~22 dB and leaves the spread within 0.5 dB of AGC, so "AGC is compressing the band"
is not the explanation for a flat sweep; the band is flat. And **146.6616 is the one
candidate**, a repeater output at +3.5 to +4.8 dB over its neighbours in both sweeps of
that span — which the FM calibration below reveals is almost certainly NOT a carrier: a
real transmitter clears its local floor by 11 dB or more, and +4.8 is noise-tail.

**`STEADY_DB` calibrated, against FM broadcast.** "Sweep something that is actually
transmitting" turned out to need no new sweep and no keyed-up repeater: FM carriers are
up 24 hours a day, and the 88-108 sweep above already held 13 of them. Measured across
four sweeps — a real transmitter clears its local floor by **+11 to +24 dB**, noise sits
at p50 0.09 dB, and the quiet 2m band's WORST bin reached +4.8:

| `STEADY_DB` | FM 88-108, stations found | quiet 2m, bins flagged |
|---|---|---|
| 3 dB | 18 | **1** (first false positive) |
| 4 dB | 16 | 0 |
| 5 dB | 15 | 0 |
| **6 dB** | **13** | **0** |
| 8 dB | 9 | 0 |
| 10 dB | 8 | 0 |
| 14 dB | 4 | 0 |

6 dB is not a compromise between the two failures — it is above one and below the other,
with 3 dB of margin to the first false positive. The shipped value was a guess and turns
out to be right, so nothing changes but its justification. Worth recording that raising
it LOSES stations, which is the opposite of the intuition: a signal's skirts fall away
smoothly from its peak, so a higher bar keeps the strong and drops the weak rather than
sharpening the distinction.

**The tuner's own DC spike does not flood the results**, which the per-bin floor was
built to ensure and nothing had checked. On the 22-hop 60 MHz sweep, 0 of 22 block
centres are reported steady; on the 8-hop FM sweep, 7 of 84 steady bins (8%) sit within
two bins of any block edge or centre.

None of this was readable from the response as first built, which returned `csv_chars`
and no CSV — which is exactly how the 1.76x claim survived. `include_csv=true` now
returns rtl_power's own output: a detector calibrated against its own summary, or
against a picture of its own summary, is calibrated against nothing.

**Off 2m: what the full range actually costs (2026-09-03, second session).** The sweep
was calibrated on 2m; five more sweeps say how far that travels.

| span | hops | bin asked / got | revisit | result |
|---|---|---|---|---|
| 146.0-147.4 (1.4 MHz) | 1 | 5 / 2.7 kHz | 1.0 s | quiet band |
| 144-148 (4 MHz) | 2 | 5 / 3.9 kHz | 1.0 s | APRS at 144.3906 |
| 440-450 (10 MHz) | 4 | 5 / 4.9 kHz | 1.0 s | 5 signals, busiest 444.575 at 23% |
| 88-108 (20 MHz) | 8 | 25 / 19.5 kHz | 1.0 s | 13 FM stations |
| 440-500 (60 MHz) | 22 | 100 / 85.2 kHz | 1.0 s | the widest span allowed |

Four things follow.

**`MAX_SWEEP_SPAN_HZ` is 60 MHz** (`deploy/sdr/listen.py`), so the tuner's 24-1766 MHz is
never one call — it is at least 29 sweeps. A 400 MHz request is refused outright. That cap
is also what makes the next line safe.

**Revisit is 1.0 s at every hop count, 1 through 22.** Every block carries identical
timestamps: rtl_power retunes WITHIN its `-i` interval rather than multiplying it. The
`_sweep_cmd` docstring claimed the opposite ("one second times the number of hops") and is
corrected. Since 60 MHz is ~25 hops, occupancy is a fraction of one-second intervals
everywhere a caller can reach — there is no wide-span regime where it quietly stops
meaning anything. `revisit_s` is reported anyway, because that is a fact about this
hardware and this cap, not about the arithmetic.

**`bin_hz` coarsens with the span**, because rtl_power picks a power-of-2 FFT per hop:
asking 5 kHz got 2.7 kHz over 1.4 MHz and 3.9 kHz over 4 MHz; asking 100 kHz over 60 MHz
got 85.2 kHz. A caller cannot pick resolution independently of width.

**The 400 kHz neighbourhood is a narrowband figure and it shows on FM broadcast.** A
station is ~200 kHz, so it occupies HALF its own default window whatever the bin size
(the window is fixed in Hz, and so is the station) — right at a median's breaking point.
Measured on the 88-108 sweep: the default found 44 steady bins in 11 channels; told
`channel_hz=200_000` (a 4.2 MHz window, where a station is under 5%) the same CSV gives 84
bins in **13** channels. The two it had been hiding, 97.71 and 105.91 MHz, are the weak
ones — exactly the failure mode. So `channel_khz` now sizes the detector's neighbourhood
as well as the folding, and off the narrowband bands it is not optional.

A cellular carrier is the same failure an order of magnitude worse — 5-20 MHz of signal
against a 400 kHz window is a baseline computed entirely from inside the transmitter —
but that is reasoned, not measured: nothing here has swept one.

**A gap this exposed.** The debug token can STOP the radio (`POST /api/debug/sdr/stop`)
but cannot restart APRS logging — `POST /api/sdr/aprs` is owner-session-only. So a
calibration sweep leaves the owner's heard log off until they flip the switch in the PWA
themselves. Per `CLAUDE.md` #10 that asymmetry is a gap to design out: whatever debug can
take, debug should be able to give back.

### Control API (owner-only, launcher-facing)

`GET /api/sdr/status` · `POST /api/sdr/tune` · `POST /api/sdr/listen` (start/stop) ·
`POST /api/sdr/record` (start/stop) · `GET /api/sdr/audio` (chunked MP3, D6) ·
`GET /api/sdr/bands` (the band table) · `POST /api/sdr/spectrum` ·
`POST /api/sdr/spectrum/tune` · `GET /api/sdr/spectrum` (SSE waterfall rows) ·
`GET /api/sdr/recordings` · `GET /api/sdr/captions` (SSE live transcription, opt-in).

**The waterfall is SSE, not the WebSocket D5 assumed (2026-09-04).** The traffic is
one-directional — rows out, nothing in — and a socket would have brought its own
handshake auth and its own CSWSH gate (`api/live.py`) to carry no message it needs.
Retuning is a POST, which is where it belongs: the picture and the control are separate
concerns with separate failures. As SSE it inherits the owner session, the same proxy
hop the audio takes, and `EventSource`'s own reconnect.

**Each row carries its own range**, so a retune needs no protocol event at all: the next
row simply describes a different band, and a client that draws what each row says is
already correct. That is what lets `POST /api/sdr/spectrum/tune` move the picture on the
session it already holds — the radio is never released in between, which is the window
in which something else takes the dongle because the owner changed band.

**Live captions (2026-09-02).** Whisper is not a streaming model, so the sidecar cuts
the live PCM it already holds into segments on the quiet gaps between transmissions
and serves them newline-framed at `GET /listen/segments`; the api transcribes each and
emits words with per-token confidence. Measured on the box, a transcription costs
~10.7 s whether the clip is 4 s or 11 s — almost entirely model load and unload — so
the caption route deliberately never frees the model, and CC is an explicit toggle
because a resident whisper shares the GPU with the chat model. Segments are squelched
in the sidecar: below a level floor nothing is sent, because whisper answers an empty
band with fluent invented sentences. Binding spec:
`../mocks/sdr-tuner/f-live-captions.html`.

**Three defects the FM dial kept after the first fix (2026-09-07).** Raising the hop
dwell settled where a peak is reported; it did not settle how many are reported, and it
did nothing at all for the picture. Measured on the box with `spectrum-probe
--section fm-broadcast`:

- **The published row overshot the band by 1.86 MHz.** Whole hops cannot tile an
  arbitrary span, so the planner rounds up: 88–108 MHz is eleven hops of 1.9875 MHz,
  reaching 109.8625. Right for the CAPTURE — the alternative is a gap at the top of the
  band — and wrong for the row. `peaks.find` judges each bin against its neighbours and
  cannot know the band ended, so it reported stations at 108.3, 108.7, 109.1, 109.4 and
  109.7, where by law there are none. The row is now trimmed to the span that was asked
  for; the capture still covers whole hops.
- **The channel grid was re-derived per row.** `raster_origin` reads the origin off the
  signals themselves, so a row whose signals happen not to agree
  (`MIN_RASTER_AGREEMENT`) reports raw measurements while the next row snaps them — and
  a viewer holding peaks across rows collects ONE station twice, under a snapped and an
  unsnapped name. That is the 50-held-for-21-on-air the owner saw. A band's grid does
  not move while a session is tuned to it, so the first row that can establish it now
  settles it for the rest of the session (`Session._grid`).
- **The striping was never a measurement problem.** The waterfall built a backing store
  at `min(devicePixelRatio, 2)`, so on a 3x phone the compositor upscaled it by 1.5 with
  its own filter. At one device pixel per measurement — what `rowPixelsFor` returns
  whenever the wanted history is taller than the display, which is the normal case —
  each row becomes one and a half PHYSICAL pixels, so consecutive rows land alternately
  on and off the pixel grid and blend differently: regular banding at exactly the row
  pitch, across the whole width, the noise floor included. That last part is what says
  it is not a signal. REPORTED as "it's not a continuous bar, it has like little blank
  sections in it", and it survived the dwell change because the dwell was never its
  cause. The backing store now matches the device (`backingRatio`).

**The surplus signals were SHAPE, and the rule came from the owner (2026-09-07).** He
read his own dial correctly — "the peaks are sometimes sidebands or multiple hits that
are very close together" — and the runs of consecutive channels in his held list
(92.1/92.3/92.5/92.7, 96.3/96.5/96.7, 98.3/98.5/98.7) are what gave it away: 47 CFR
§73.207 minimum separations mean two local first-adjacent stations essentially cannot
exist.

Three researchers were run against a 45 s integration of the real dial. **21 stations
are real and all 21 were already being reported — recall was never the problem.** The
16 surplus entries are two families of eight: SKIRTS of a loud neighbour (monotonic, no
inboard dip) and BIRDIES (18-28 kHz spikes with row-to-row variation of 0.25 dB against
a band median of 0.29 — no modulation, so not an emission).

What was measured, and what it rules out:

- **Threshold tuning is dead.** Simulated against the repo's own measured per-bin sigma:
  8 dB, 6 dB and even 4 dB all give ZERO thermal false alarms per row. Not one surplus
  entry is a noise false alarm, and raising `SNR_DB` only deletes real stations (18 of
  21 survive at 10 dB, 11 at 12 dB).
- **A cap cannot separate them.** The weakest real stations sit 2-3 dB BELOW several
  artefacts in the ranking; top-N keeps 19 of 21 while removing only 14 of 16.
- **Intermodulation is ruled out**, though it looked plausible: a 3rd-order product of
  wideband-FM carriers is 450-540 kHz wide and the surplus features measure 18.8-93.8
  kHz. Correlation between predicted IMD weight and measured excess is -0.18 — the
  wrong sign.
- **The first-adjacent rule the owner implied is right 7 times in 8 and fails once**, on
  101.1/101.3, which is a genuine adjacent pair (a 3.17 dB notch between them, where
  splatter pairs measure 0.09-0.50 dB). It is not tunable: for any adjacent pair the two
  margins sum to exactly zero, so such a rule can NEVER report both. Proved over all 99
  pairs in the band.

**What shipped instead: topographic prominence and width** (`MIN_PROMINENCE_DB`,
`MIN_WIDTH_SHARE`). Prominence is the "peak excursion" every spectrum analyser has and
this file did not — Keysight ships 6 dB by default — and it is what a skirt cannot have,
being monotonic. Width is what a birdie cannot have. **Verified against the real dial
with the shipped code: 21 of 21 real stations kept, 15 of 16 surplus rejected**, the
survivor being one splatter shoulder at 105.9. Neither test subsumes the other: the
shelves (96.7, 98.7) clear the width floor and fail prominence, while the birdies are
MORE prominent than the weakest real station and fail width.

Width is asked only where a band plan says what wide means; prominence needs no plan and
always applies. That is what keeps the rule safe on 2 m repeater pairs and 25 kHz
airband, where adjacent occupancy is the design intent and a first-adjacent rule would
have been actively harmful.

**And the decision moved off the max-hold row.** `held_row` is the picture; `mean_row`
is the decision. Max-of-N lifts an empty guard band as much as a carrier, flattening the
very shape this rule reads — measured, a real station's margin over its guard bands is
+2.03 dB on the linear mean of four sweeps and -0.05 dB on the max of the same four.

One bug found on the way and fixed: `Session._grid` was never cleared on a retune, so a
session moved from the FM dial onto a 15 kHz plan snapped every label 10 kHz off its
channel, confidently and stably.

**A second "bug" was reported and was not one.** The analysis found `measured_hz` half a
bin low, and the half-bin was added — then C20's own test
(`test_the_probe_and_the_PEAK_FINDER_are_on_one_frequency_grid`) failed, correctly:
`iq.Spectrometer.start_hz` is bin 0's CENTRE, because `fftshift` puts DC in bin `n // 2`,
so `start_hz + index * bin_hz` already addresses bin `index` exactly. The 5.14 kHz offset
that was measured is a fact about `rtl_power`'s `hz_low`, which IS a band edge, and not
about this path. Reverted, and the reason is now recorded in `peaks.find` so it is not
"fixed" a third time. C20 exists because those two grids once sat half a bin apart; it
earned its keep again here.

**The labels were stale, and it looked like more sidebands than there were
(2026-09-07).** With prominence and width shipped the dial read 22 signals, of which
only two were surplus — but five of the real ones wore RAW frequencies (96.5 as
"96.494", 106.1 as "106.084") while the weaker stations either side of them read as
clean channels. `mergePeaks` keeps the frequency of a signal's STRONGEST sighting, which
was right when nothing snapped and became staleness the day the box started placing
signals on a grid: a station's all-time maximum is usually EARLY, often before the
session had enough signals to establish the grid, so the loudest stations were exactly
the ones frozen with a pre-grid label. A snapped `hz` is a channel and cannot wander —
`measuredHz` carries the wander now — so the newest label is adopted whenever the box
labelled it, and the strongest sighting still supplies the LEVEL and still supplies the
frequency where no grid exists.

**The guard margin is measured and deliberately NOT wired in.** On the 45 s integration
it is the better rule — 21 of 21 real kept, 16 of 16 surplus rejected, where
prominence+width leaves one — but the worst real station clears by 1.06 dB against a
best artefact of 0.33, and on a short window that gap closes and inverts (measured on
single max-of-4 rows: worst real -0.14 dB, best surplus +1.50). The decision row is a
mean of `HOLD_SWEEPS` sweeps, about 1.8 s, which is nearer the short case. Wiring it in
at this window would delete real stations to remove two artefacts. `guard_margin_db`
exists, is tested, and `_has_shape` does not call it; what it needs is a ~7 s decision
window separate from the picture's, which costs a scanner band that long to report a
burst and is a decision to take deliberately.

**Still open:** the held list in the PWA has no expiry, so it is a union over the whole
session and one false positive is permanent (`MAX_PEAKS` is 24, so a 37-entry list can
only be accumulation). A duty cycle would replace it honestly. And the receiver is
COMPRESSED — 27 dB of dynamic range for the whole dial and a noise floor that climbs
4.5 dB from 88 to 104 MHz, which is front-end hash, not thermal noise. Every margin here
is small because of that; an FM band-pass or more attenuation would widen them all more
than any detection rule can.

**The striping was the MODULATION, and the picture now holds the strongest of four
sweeps (2026-09-07).** Measured with the instrument below, 27 rows over 12 s on the FM
dial: **zero** bins missing, and the band's strongest carrier standing up in **14 of 27
rows** with a **31.6 dB** swing. So no data was being lost — the same station was simply
measured and not measured, row after row, which draws as a dashed line. A hop looks for
6.8 ms and a wideband-FM carrier sweeps its own ±75 kHz continuously, so every segment
inside that window sees one instant of a modulation that moves on the audio's timescale;
no amount of averaging INSIDE a hop can fix it.

Three ways out, and the owner chose the first: **a rolling max-hold of `HOLD_SWEEPS`
sweeps** (~1.8 s of independent looks, same row rate, same resolution — a row now means
"the strongest reading in the last ~1.8 s", so a burst leaves a short tail); a longer
dwell per hop (keeps "a row is one measurement" exactly true, drops the rate from ~2.25
to ~0.7 rows a second); or coarser bins on the dial, so a carrier's whole deviation stays
in one bin (free and rock solid, but one bin per channel). The max-hold is the reduction
the PWA already makes when it stacks rows (`holdInto`), so it is the house idiom.

**The striping outlived the renderer fix, and was MEASURED rather than guessed
(2026-09-07).** `backingRatio` was aimed at a compositor artefact and the owner still
reports "blank sections". Two mechanisms produce the same picture and have opposite
fixes — rows that lost bins (a hop that drops a USB block leaves NaN, which `peaks.find`
skips and the PWA paints transparent, both deliberately) versus a carrier measured in
some rows and not others (a 6.8 ms look at a wideband-FM station is a look at its
MODULATION, which sloshes between bins slower than the window). Guessing between them
from a photograph has now cost two wrong fixes, so `spectrum-probe` answers it:
`steadiness` reports `gaps` (bins no measurement reached) and `carrier_seen_in` (how
many rows the last row's strongest bin still stood up in). Read it before choosing.

The owner's own hypothesis — publish only a fully swept row — is already what happens:
`_sweep_hops` fills every hop of the array and publishes once. It is not the cause.

**A channel can only be reported once (2026-09-07).** Snapping can collide: two runs
120–140 kHz apart are wider than `SAME_SIGNAL_SHARE` so `find` keeps them separate, and
each within `MAX_RASTER_PULL` of the same channel so both snap onto it. The viewer holds
peaks by frequency and kept both — REPORTED as two pills reading 106.100 side by side.
`_one_per_channel` keeps the strongest.

**Where the ~8.3 s actually came from (2026-09-07).** It was read as a pipeline
latency for five days and it was not one. `playSdrAudio` fires from the session poll the
moment a listening session appears — not a user gesture — so a phone refuses the
`play()`. The refusal does not stop the LOAD: the element went on pulling the live
stream into its buffer, unheard, until the owner reached the play button, and playback
then began at media position zero and stayed exactly that far behind the air for the
rest of the session. The "constant, stable, not drifting" 8.3 s was the length of the
wait before someone pressed play. Three changes, all shipped together:

- **A refused `play()` drops the src** (`sdrAudio.ts` `refused()`), so nothing
  accumulates unheard and the owner's tap starts where the air is now.
- **The encoded read is sized in TIME** (`listen.py` `AUDIO_CHUNK_S`). `_pump_audio`
  read `_CHUNK` = 4096 bytes, sized as 128 ms of 16 kHz PCM and reused on the compressed
  side where the same bytes are 512 ms at 64 kbps — and `read()` blocks until full, so
  every listener waited half a second for audio the encoder had finished with. The same
  correction drops the drop-a-slow-subscriber queue from 32 s to 4 s, which is what the
  comment above it always claimed it was.
- **The transport measures rather than asserts.** `sdrAudioLag()` reads
  `buffered.end − currentTime`, and the tag that said LIVE straight through eight
  seconds of delay now says `LIVE −8s` when it is behind. The owner has no terminal
  (CLAUDE.md #10), so a figure nothing on screen could contradict is a figure that stays
  wrong for five days.
- **A drift watchdog rejoins the live edge** (`sdrAudio.ts` `checkDrift()`). `<audio>`
  offers no way to ask for a short buffer: it plays what it has at 1×, so a stall — a
  lock screen, a lost second of network — is paid back as PERMANENT delay that nothing
  ever catches up. Re-pointing at the stream is the one move that does, because MP3 has
  no header and the sidecar simply starts sending from where the air is now. Measured
  against the stream's OWN floor rather than a fixed ceiling: a browser that always
  buffers three seconds is not late, and a ceiling under whatever it chose would put a
  gap in the audio every few seconds chasing a delay that was never going away. It runs
  ahead of the analyser in the sampler, not behind it — hanging it off the Web Audio tap
  would have meant a device that refuses an AudioContext silently loses the correction
  too, on the very device most likely to need it.

**What remains, and what it is bounded by.** The audio path's own contribution is now
~64 ms of chunking plus the encoder; everything else is the browser's start-up buffer,
which `<audio>` does not expose a knob for. The watchdog bounds it from growing. Going
lower than whatever the browser picks needs Media Source Extensions — appending MP3
frames and driving the buffer directly — which is a real build and is not taken here.
The spectrum is synced to whatever that residual turns out to be, exactly, so the
residual costs sync nothing.

**Caption timing (2026-09-02, revised 2026-09-07).** Two lags decide whether a caption
lines up with the speech, and they point opposite ways. Playback sat a constant ~8.3 s
behind the live edge (measured in Chromium against the real sidecar over 130 s — stable,
not drifting, and diagnosed above),
so a caption shown when it *arrives* lands seconds before the words are heard; the
client therefore holds each one until `sdrHeardAt()` — the box-clock time of the audio
at the speaker, anchored on `started_at + elapsed_s` — reaches its start. Against that,
the api must produce captions in less than those 8.3 s or holding cannot help: read in
step with whisper, each ~9.8 s call stalled the reader, the sidecar's 8-deep queue
filled behind it and the captioner settled *permanently* ~40 s back. So reading runs on
its own task and whatever has piled up is transcribed as ONE merged clip — free,
because whisper's cost here is flat in clip length, and unlike keeping only the newest
it drops no words. What remains is the floor: a caption cannot exist before its segment
ends, so segment length plus transcription is how far behind the words it can be — with
`large-v3-turbo` at ~9.8 s that floor is ~20 s, well past the ear's 8.3 s, so captions
still trail the speech. Closing the rest needs a deliberately delayed audio path (prime
each listener with a rolling MP3 backlog so playback starts further back) or a smaller
model; both are owner decisions. **The delayed-audio option is now REFUSED** — asked
2026-09-07, the owner chose real-time audio and spectrum over captions that line up. The
hold in `sdrCaptions.ts` stays and simply has less to hold: with playback near the air,
a caption's remaining lag is whisper's floor, which is the honest one.

The ~9.8 s is INFERENCE, measured across 26 consecutive calls with the model resident.
An earlier reading of the same flat-in-clip-length number blamed model load/unload; it
is flat because whisper.cpp pads every clip to a 30 s window. Residency is still right
— unloading adds the load on top — but for that reason, not the one first written down.

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
- **HF below 24 MHz — LISTENING SHIPPED 2026-09-04; `rtl_power` SWEEPING NEVER WILL.**
  The open question here ("whether `rtl_power` can even be put into that mode") is
  answered, and the answer is no. `rtl_power -D` hardcodes
  `verbose_direct_sampling(dev, 1)` — the ADC's **I branch** — while the NESDR SMArt v5
  wires **Q** through its on-board diplexer (Nooelec's datasheet block diagram; no
  hardware mod, which is what makes this a software gap rather than a hardware one).
  `rtl_fm` lets the branch be chosen, so `-E direct2` reaches it and listening works.
  Sweeping would need a patched C tool and therefore a compiler in the image.
  ⟲ **"And therefore no waterfall either" was the wrong conclusion, and
  `SDR_IQ_SPECTRUM_PLAN.md` F8 removed it.** That inference held only while the picture
  WAS `rtl_power`; the live tier now reads raw I/Q and does its own FFT, which sets
  direct sampling mode 2 at runtime. So `tuner.sweepable` (the survey) and
  `tuner.viewable` (the picture) are two predicates with two answers, and shortwave is
  drawable and still not surveyable.
  The range is modelled as **two paths, not one lower floor** (`jbrain/sdr/tuner.py`):
  below the tuner it is powered down entirely, so there is **no gain control** at all.
  ⟲ The third fact this used to state — a `mirrored` flag above 14.4 MHz — was both
  dead and mis-stated, and F4 replaced it: every section stops at or below 14.35 MHz so
  the flag was False for all ten HF rows, while **all ten carry a reversed image** of
  `28.8 MHz − f` summed into the same bins. The band table now names the folded band
  (`image_start_hz`/`image_stop_hz`) instead of raising a flag nothing ever set.
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
