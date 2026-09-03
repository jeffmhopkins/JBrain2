# Scanning the bands — pathfinding tools before a survey

> **Status:** Draft, unreviewed · **Last verified:** 2026-09-03 · **Waves:** S0◻️(the
> lease learns about devices) S1◻️(`sdr_scan` — what is busy) S2◻️(`sdr_waterfall` —
> what it looks like) S3◻️(`sdr_identify` — what it *is*) S4◻️(the day survey + `sdr_activity`).
> **S1–S3 are the ask: basic tools first.** S4 is sketched here so the earlier waves do
> not paint it into a corner, and is not committed to.

The owner asked how to get spectrum data to a model so it can "scan 2m and 70cm and
identify common signals over a day". The honest answer changed the shape of the work,
so it is recorded before the waves.

## The measurement that decides the representation

A waterfall image is the worst option available, and the arithmetic is not close.

2m alone at 5 kHz bins is 800 buckets. One row per second for a day is 86,400 rows —
**69 million cells**. A vision model sees roughly 1.1 megapixels after downsampling. The
frequency axis survives that fine (800 into ~1568 px is oversampled). **The time axis
does not:** 86,400 seconds into 1,568 pixel rows is 55 seconds per row, so a 10-second
transmission is 18% of one row. Averaged against 45 seconds of noise floor it lifts that
pixel by about a dB, and disappears.

So the one thing that survives downsampling is the long-term average, and the thing
being hunted is short bursts. **The picture destroys exactly the events it is meant to
show.** Tiling helps — 24 hourly images give ~2.3 s/px, where a 10-second transmission
is 4 px tall — but the model still cannot read "146.940 MHz at 16:42:03" off a plot, and
the DSP already knows it exactly.

| Representation | Size for one day of 2m | Verdict |
|---|---|---|
| Waterfall PNG | ~1 Mpx after downsampling | Destroys the events; coordinates unreadable |
| Numeric power grid | 69 M cells | Exact, and absurd to hand a model |
| **Event list** | ~100 KB | `{freq, start, duration, bandwidth, snr}` |
| **Channel summary** | ~30 rows | What a model should actually be given |

**The box measures, the model interprets.** This is the same split as `classify.py` and
`explain.py` versus jerv, for the same reason: ask a model to measure and it guesses; give
it measurements and ask what they mean and it is genuinely good. The picture is for the
*owner*, and for narrow visual questions a second pass can ask.

## The second measurement: the bands are channelized

2m repeater outputs sit on 15 kHz spacing, 70cm on 25 kHz. This is not 30 MHz of
continuum — it is a few hundred possible channels, so the data model is **occupancy per
channel over time**, which is a table.

And it changes the capture strategy. The active part of 2m — repeater outputs
145.2–145.5 and 146.61–147.39, plus the simplex calling frequencies — spans about
**2.3 MHz**, and an RTL-SDR gives ~2.0–2.2 MHz clean out of 2.4 MS/s. So 2m's busy
subband is *at the edge of a single tune*: with a channelized demodulator it can be
watched continuously rather than swept, with no revisit interval and no missed short
transmissions. 70cm does not cooperate — 440–450 is 10 MHz, five hops — so there the
sweep stands, and the sweep is adequate: at 0.5 s dwell the revisit is ~2.5 s, so a
20-second repeater exchange is caught eight times. Rare one-off bursts are missed, but
those are not "common signals", so the weakness does not touch the goal.

## Non-negotiables this inherits

Root `CLAUDE.md` in full, and from `APRS_CONTROL_PLAN.md` the two that bite hardest here:

1. **Every tool below TAKES THE RADIO.** `aprs_recent` is `permission: read` because it
   reads a table. A 60-second scan is 60 seconds of APRS logging not happening. These sit
   in `sdr_listen`'s tier, hold the lease, and must say what they displaced.
2. **The trust split is not where APRS put it.** The *measurements* are ours — power,
   bandwidth, occupancy — and need no envelope. Anything **decoded** is a stranger's
   words: a CW callsign forges exactly as easily as an APRS callsign, and transcribed
   voice more so. A tool returning both returns them differently, with the decoded half
   inside `<untrusted_external_data source="heard-over-the-air">`.

Third, from this plan: **every duration and span is capped server-side.** An agent will
ask for `seconds: 3600`, because nothing in its training says the radio is scarce.

## Waves

**S0 ◻️ — the lease learns there is more than one radio.** Blocking, and larger than it
looks. `Tuner` holds exactly ONE `Session` and is device-blind: no serial, no index, no
enumeration. `/capture` already accepts a `serial` and passes it to `rtl_fm`, but the
streaming lease does not, so a second dongle buys nothing until the lease can address
devices. Wave: enumerate devices, key the lease by device rather than globally, teach
`SdrBusy` to say *which* radio is busy, and add a third `purpose` (`survey`). The PWA's
single-radio affordances (`c-single-dongle.html`, shape A) assume one tuner and will need
a round of their own — **GUI gate, not yet opened.**

**S1 ◻️ — `sdr_scan`: what is busy.** Sweep a range, return only buckets above the noise
floor, with occupancy as a percentage of the sweep rather than a peak in dB — a one-off
burst and a channel busy half the time have identical peaks. Built on `soapy_power` or
`rtl_power_fftw` rather than our own FFT loop. Returns rows, never the grid.

**S2 ◻️ — `sdr_waterfall`: what it looks like.** Narrow span, short window, returns a
chat image **and** the peak list, so the model never reads coordinates off pixels. Span
and duration hard-capped: this is the tool whose misuse is a ten-minute lease and an
illegible picture.

**S3 ◻️ — `sdr_identify`: what it *is*.** The expensive look at one frequency, and where
the value is. Bandwidth, carrier duty cycle, transmission timing — and the CW ID.
**Repeaters identify themselves in Morse every ten minutes by regulation**, so
`multimon-ng` on the squelched audio hands back a callsign rather than a database guess.
This is the wave that turns "something is on 442.850" into "W4ABC". Digital modes
(P25/DMR/D-STAR) carry IDs in-protocol too; that is a follow-on, not this wave.

**S4 ◻️ (sketched, not committed) — the day survey and `sdr_activity`.** A scheduled job
that runs the sweep continuously, detects events against a *rolling percentile* noise
floor (it drifts with temperature and time of day, so a fixed threshold drowns in false
positives at 3 a.m.), and aggregates per channel per day. `sdr_activity` is then a
`read` tool over that table, exactly as `aprs_recent` is over the packet log. Only this
wave needs new storage; S1–S3 need none.

## What is bought rather than built

- **`soapy_power` / `rtl_power_fftw`** — the sweep. `rtl_power` is the ancestor; the
  rewrites have better FFT handling and finer integration control.
- **RTLSDR-Airband** — channelized multi-channel NFM with per-channel squelch, which is
  what makes the "watch all of 2m at once" strategy possible. Built for aviation AM;
  **its NFM path is the thing to verify before S4 leans on it.**
- **`multimon-ng`** — the CW decoder for S3, among many other modes.
- **RepeaterBook API** — turns a detected frequency into a named machine, when the box
  knows where it is. A cross-reference, not an AI task.
- **SigMF** — a metadata standard whose *annotation* shape (frequency range, time range,
  label) is precisely the event schema here. Worth stealing even if no SigMF file is ever
  written.
- **Not** GNU Radio (`gr-inspector`) — a large dependency for what energy detection does
  in fifty lines of numpy. **Not** TorchSig or RadioML modulation classifiers: bandwidth,
  duration, periodicity and a decoded callsign get most of the way, and AMC models trained
  on synthetic data are fragile at real SNRs on real hardware. Held in reserve.

## What has to be measured before S4 is designed

An IQ capture from the owner's actual antenna. The RTL-SDR has a DC spike at every tuner
centre frequency, known spur patterns, and front-end overload from strong nearby
transmitters (FM broadcast especially) that desensitizes across the band and *looks like
activity*. A detector built against assumed noise is a detector that reports the tuner's
own artifacts as signals. Fixed gain, no AGC, and a notch on the centre bin — but the
thresholds come from the capture, not from this document.

## Open questions

1. **Is S0 in scope, or does this plan wait for it?** S1–S3 work on one dongle today —
   they just contend with APRS logging. S0 is the wave that makes contention tolerable,
   and it drags a GUI round with it.
2. **Does `sdr_waterfall` earn its place at all in the basic set?** Its honest use is
   narrow and visual; the owner may want it mainly for himself, which is a PWA feature
   rather than a tool.
3. **Where do the tools' results live?** S1–S3 return to the turn and vanish. A scan
   worth running is probably worth keeping, but that is storage, and storage is S4.
