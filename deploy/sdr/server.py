"""The `sdr` sidecar: tune a USB software-defined radio, return audio.

The only container that touches the radio. It owns the devices because the arbitration
has to happen somewhere only one process can see: one session per RADIO, so listening,
sweeping and recording are mutually exclusive PER DONGLE and a second caller for a held
one is refused by name (docs/plans/SDR_RADIO_PLAN.md §4.2, APRS_CONTROL_PLAN.md P0b).
It was one holder per box until there were two dongles, which is the same thing while
there is one. Arbitrating here rather than in the api means the guarantee holds no
matter who calls — and there are four callers.

**Egress-free by topology.** Compose puts this on a network declared `internal: true`,
so the container has no route off the box. A radio receiver has no business reaching
the internet, and saying so with the network rather than with policy makes it true
regardless of what runs in here.

**No model input ever reaches this process as a URL or a path.** The api passes a
frequency and a mode, both validated against the tuner's real range before they get
here, and validated again below. The `stream.py` SSRF guard is untouched by any of
this — see §4.4 of the plan.

Dependencies are APT ONLY, NO PIP — a rule that used to say stdlib-only, and
`Dockerfile.sdr` argues the weakening. The HTTP surface is `http.server` from the
stdlib, the radio work is `rtl_fm` (from the `rtl-sdr` package) over a pipe, and numpy
and SoapySDR come from Debian for the spectrum path. What rtl_fm hands back is a 16 kHz
mono WAV, which is exactly what whisper wants — no resampling stage, a convenience of
its native output rather than a coincidence.
"""

from __future__ import annotations

import base64
import io
import contextlib
import json
import math
import os
import queue
import shutil
import struct
import subprocess
import time
import urllib.parse
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

import iq
import listen
import radio
import usbdev
from listen import (
    PURPOSE_APRS,
    PURPOSE_LABEL,
    PURPOSE_LISTEN,
    PURPOSE_SPECTRUM,
    PURPOSES,
    SWEEPING,
)
from listen import SdrBusy as ListenBusy
from listen import SdrError as ListenError
from listen import AUDIO_CONTENT_TYPE, AUDIO_RATE, Tuner


# The tuner's range and the mode allowlist come from `listen`, which is where the
# radio lives — they were declared again here, and a second declaration of a fact is a
# place for the two to disagree. One of them already had: this file also carried
# `WBFM_SAMPLE_RATE = 171_000`, unused, against the 192_000 `listen` measured and
# documented at length, so the dead copy contradicted the live one in the same repo.
MIN_HZ = listen.MIN_HZ
MAX_HZ = listen.MAX_HZ
MODES = listen.MODES

# 16 kHz mono is whisper's native input AND rtl_fm's, for narrowband. Wide FM needs a
# higher demodulation rate to sound right, so it is captured at 32 kHz and told to
# resample down — rtl_fm does that itself with `-r`.
NARROW_RATE = 16_000

MAX_SECONDS = 120
#: How long rtl_fm gets to run its SIGTERM handler — cancel the USB transfer, close the
#: device — before a capture escalates to SIGKILL. The handler needs milliseconds; this
#: is slack for a loaded box, and it matches the live path's own grace in
#: `listen.Session._kill` so the two cannot drift into different teardown behaviour.
_SHUTDOWN_GRACE_S = 2


# Every radio this box is holding and what for: the live sessions, plus the one-shot
# `capture` path, which reserves a radio without being a session. ONE registry because
# the two used to be two, and the seam showed — capture refused while a session held the
# radio, but `start` never looked at the capture lock, so a listen could open a dongle
# mid-recording. Per radio, so APRS on the long wire and the tuner on the desk whip run
# at once rather than taking turns.
TUNER = Tuner()


class SdrBusy(RuntimeError):
    """Something else holds the radio this call would open. One radio, one caller."""


class SdrError(RuntimeError):
    """rtl_fm could not tune or produce audio."""


def _wav(pcm: bytes, rate: int) -> bytes:
    """Wrap raw signed 16-bit mono PCM in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)
    return buf.getvalue()


#: How long a spectrum probe watches by default, and the most it will. Seconds, not
#: minutes: it holds the radio the whole time, and every question it answers — which
#: engine ran, whether the width is exact, what the frame rate is — is answered by the
#: first few frames. The cap is what stops a caller turning a diagnostic into a lease.
SPECTRUM_PROBE_S = 3.0
SPECTRUM_PROBE_MAX_S = 15.0

#: How long a LISTEN probe holds the radio, and the most it will. Longer than the
#: spectrum one's default because what it is measuring is continuity: a dropped USB
#: buffer is a click rather than a missing row, and a two-second look is too short to
#: say whether a stream that started cleanly stays clean.
#: How much of a buffer has to hit the rail before it is clipping rather than the
#: impulse noise every FM receiver makes on a weak signal.
#:
#: MEASURED 2026-09-05, which is the only reason it is 5% and not a guess. This was 1%
#: first and fired on a perfectly healthy weak signal. Against the same chain:
#:
#:   162.550 nfm, 14 dB SNR   1.0-1.25% clipped, RMS 0.40   <- clicks
#:   96.5 / 99.3 / 104.1 wbfm, 31-40 dB SNR   0.0% clipped, peak 0.37-0.42
#:
#: Zero at high SNR with 60% of headroom to spare says the deviation scaling is right
#: and the clipping at 14 dB is the noise, not the gain — and it did not move with the
#: tuner gain either (1.00% on AGC, 1.19% at 40 dB), because a discriminator is blind
#: to amplitude. A gain that really was too high is an order of magnitude worse than
#: this: 1.5x deviation clips nearly half a sine.
#:
#: **The 162.550 row was not a weak signal. It was NO signal**, and the "14 dB SNR"
#: was the front end's own roll-off measured against itself (`demod.VIEW_SHARE`). The
#: threshold it justifies is still right — 5% separates a chain clipping from one
#: making clicks — but the row is here as the reading it turned out to be: an empty
#: channel, whose 1% of samples at the rail is what pure noise through a discriminator
#: does. `CHANNEL_SIGNAL_DB` below is the check that now says so.
CLIPPING_FRACTION = 0.05

#: How far the strongest bin must stand over the channel's own floor before the probe
#: agrees there is a station there. Mirrored from `SIGNAL_OVER_DB` in
#: `frontend/src/sdrTuning.ts` on purpose: the strip stops drawing a signal at the same
#: place the probe stops claiming one, so the picture and the verdict cannot disagree.
CHANNEL_SIGNAL_DB = 6.0

#: ...and where narrowband FM stops being listenable. Below roughly this, the noise
#: wins the discriminator often enough that the audio is more click than voice — the
#: FM threshold effect, and the reason a receiver can go from clear to unusable over
#: about three decibels. Reported rather than enforced: a marginal signal is still one
#: the owner may want to strain to hear.
FM_THRESHOLD_DB = 12.0

LISTEN_PROBE_S = 5.0
LISTEN_PROBE_MAX_S = 20.0
#: Above this a live spectrum is running faster than `rtl_power` can, whose interval is
#: clamped to `>= 1s` in its own C. Not a target, a DISCRIMINATOR: it is how a frame
#: rate tells you which engine produced it, independently of what the session says.
RTL_POWER_CEILING_FPS = 1.5


def _channel_floor(frame: "listen.Frame") -> float:
    """The noise this channel sits on, from the bins OUTSIDE the passband.

    Not the row's median, which is what this reported first and which is not a noise
    floor on a channel view: the row is cropped to twice the passband, so the signal
    fills half of it and the median lands on the signal's own shoulder. Measured
    2026-09-05 that made 162.550 — NOAA weather, transmitting continuously — read
    7.9 dB "over the floor", which sounds like a dead receiver and is really a
    reference taken from inside the thing being measured.

    The outer bins are the honest reference: they are past the demodulator's passband
    by construction, so nothing the radio is listening to is in them.

    **Which bins those are is not symmetric** (C14). The row straddles the dial and the
    passband need not: `usb` hears +300..+3400 Hz of it. Slicing an equal strip off each
    end — which this did — put the top of a `usb` passband in the "noise", lifting the
    floor with the signal itself and hiding a weak station. `sdrTuning.ts` grew the same
    fix in W6b and this twin was missed; they answer the same question for the same rows,
    and a floor measured differently in the two places is a disagreement waiting for the
    marginal case."""
    half = frame.passband_hz / 2.0
    low = frame.passband_centre_hz - half
    high = frame.passband_centre_hz + half
    outside = [
        value
        for index, value in enumerate(frame.db)
        # The bin's own centre, as an offset from the row's middle — which is the dial.
        if not (low <= (index + 0.5 - len(frame.db) / 2) * frame.bin_hz <= high)
    ]
    if not outside:
        outside = list(frame.db)
    outside.sort()
    # A LOW PERCENTILE, not the median (C21). The row reaches four times the passband, so
    # on the FM dial its outer thirds are where the 200 kHz raster puts the neighbour —
    # and a neighbour's skirt can fill a third of this ring, which is enough to drag a
    # median off the noise and onto the edge of another station.
    #
    # MEASURED ON AIR 2026-09-06: beside a carrier at 96.494 MHz, tuning 96.3 read a ring
    # floor of -46.9 dB where an empty channel at 107.9 read -49.1 — two decibels of
    # "noise" that was a station, taken off every SNR reported there. The quartile sits
    # in clean noise whether or not a neighbour is present, and on an empty ring it is
    # within a decibel of the median it replaces.
    return outside[len(outside) // 4]


def _channel_centre(frame: "listen.Frame", middle: float) -> tuple[float, int]:
    """Where the signal in a channel row actually sits, as an offset from `middle`.

    The MIDPOINT OF THE 6 dB SHOULDERS, not the argmax, and the same rule
    `frontend/src/sdrTuning.ts` draws with — deliberately the same, because a probe
    that measured "is it centred?" differently from the picture would disagree with it
    on exactly the marginal cases both exist to catch.

    The argmax is not usable here: an FM carrier's top is flat, so the loudest bin is
    wherever the noise happened to peak across a plateau a hundred bins wide, and on a
    32 kHz row that is an answer up to a third of the view out. It would fire the
    "mixer and tuning disagree" finding on a healthy radio and mask a real offset error
    on a sick one.

    Walks OUT from the peak rather than scanning the row, so a second signal inside the
    view is not swept into the first one's width.

    **The peak is looked for INSIDE THE PASSBAND, not across the row** (C21). The row is
    four times the passband's reach, so on the FM dial its outer thirds are exactly where
    the 200 kHz raster puts the NEIGHBOUR — and a global argmax there answers "where is
    the signal in this channel?" with a different station.

    MEASURED ON AIR 2026-09-06, tuning either side of a carrier at 96.494 MHz: at 96.3
    the strongest bin in the row sat at **+157,969 Hz** and at 96.7 at **-161,250 Hz** —
    both of them the same neighbour, 158 kHz outside a passband 90 kHz wide. The tuning
    readout would have said "1.6 kHz high" about a station it is not listening to, and
    the "tune to it" button would have moved the dial onto it."""
    half = frame.passband_hz / 2.0
    lowest = frame.passband_centre_hz - half
    highest = frame.passband_centre_hz + half
    inside = [
        index
        for index in range(len(frame.db))
        if lowest <= (index + 0.5 - len(frame.db) / 2) * frame.bin_hz <= highest
    ]
    peak_at = max(inside or range(len(frame.db)), key=lambda i: frame.db[i])
    edge = frame.db[peak_at] - 6.0
    low = peak_at
    while low > 0 and frame.db[low - 1] >= edge:
        low -= 1
    high = peak_at
    while high < len(frame.db) - 1 and frame.db[high + 1] >= edge:
        high += 1
    centre_bin = (low + high) / 2.0
    # NO half bin (C20). `iq.Spectrometer.start_hz` is bin 0's CENTRE — `fftshift` puts
    # DC in bin `n // 2`, so `start_hz + i * bin_hz` addresses bin `i` exactly, which is
    # what `Spectrum.stop_hz`'s own comment says and what `peaks.py` does. Adding half a
    # bin here put this readout and the box's own peak frequencies on two grids half a
    # bin apart: 46.9 Hz on a 93.75 Hz channel row, 293 Hz on a band one. Small, and
    # exactly the kind of thing that is never traced because both numbers look right.
    return frame.start_hz + centre_bin * frame.bin_hz - middle, peak_at


#: How many of a band row's signals the probe reports. Enough to say what the dial
#: looks like; not so many that a quiet band's noise fills the answer.
BAND_REPORT_PEAKS = 8


def _band_report(
    rows: list["listen.Frame"], gain_db: float | None = None
) -> dict[str, Any]:
    """What else was on the air, from the SAME capture that made the audio.

    The reading W3 exists to make possible: before it, "listen to 162.55" and "what is
    on 2 m" were two sessions and one radio, so the second question could only be
    answered by giving up the first. Taken from the LAST row rather than pooled across
    them, because pooling would report a band that never existed at any one moment.

    `gain_db` is None when the tuner is on its own loop, and then there are NO peaks —
    not because the picture is worthless but because its peaks would be. MEASURED ON
    AIR 2026-09-06: 162.550 under AGC ran to -7.0 dBFS and the R820T2 answered with a
    symmetric spur comb reported as seven stations. `--gain 30` is how to ask this
    question and get an answer about the air."""
    if not rows:
        return {"rows": 0, "gain_db": gain_db, "peaks": []}
    last = rows[-1]
    return {
        "rows": len(rows),
        "start_hz": last.start_hz,
        "stop_hz": last.stop_hz,
        "bin_hz": last.bin_hz,
        # The tuner's own loop, said plainly, with the sentence that reads the empty
        # peak list. A reader who does not know the gain cannot tell "no stations" from
        # "no measurement", and those are opposite answers. Not a FINDING — nothing
        # failed, and `ok` means the demodulator works — but not a silence either.
        "gain_db": gain_db,
        **(
            {}
            if gain_db is not None
            else {
                "note": (
                    "no peaks: this session's tuner is on its own AGC loop, where a "
                    "strong carrier drives the front end into inventing symmetric "
                    "neighbours (MEASURED: 162.550 at -7.0 dBFS produced seven). The "
                    "picture is real and its levels are relative; ask again with a "
                    "gain to measure what is on the band."
                )
            }
        ),
        "peaks": [
            {
                "mhz": round(peak["hz"] / 1_000_000, 4),
                "db": round(float(peak["db"]), 1),
                "over_db": round(float(peak.get("over_db", 0.0)), 1),
            }
            for peak in last.peaks[:BAND_REPORT_PEAKS]
        ],
    }


def _retune_report(
    turned: dict[str, Any],
    frames: list["listen.Frame"],
    before_token: int,
    after_token: int,
    retune_hz: int,
) -> dict[str, Any]:
    """Did the radio MOVE, or was the pipeline rebuilt under the session? (A2)

    "The session id survived" cannot tell those apart — it survived a rebuild too, by
    design. Two things can:

    **`stream_rebuilt`** compares `setupStream`'s handle either side. `setupStream` is
    called exactly once per `Radio`, so a token that did not change is proof the stream
    was never torn down, which is a claim no timing measurement can make.

    **`worst_gap_ms`** is the longest silence between two consecutive channel rows, and
    it is the number the OWNER experiences: the rows come off the same buffer as the
    audio, at `TARGET_FPS`, so the gap between them is the gap in the sound. Reported
    beside `median_gap_ms` because a probe that only gave the worst has no scale to read
    it against — a rebuild is several times the frame period and a moved radio is about
    one."""
    at = turned.get("at") or 0.0
    stamps = sorted(f.at for f in frames)
    gaps = [
        (later - earlier, earlier)
        for earlier, later in zip(stamps, stamps[1:], strict=False)
    ]
    out: dict[str, Any] = {
        "to_hz": retune_hz,
        "accepted": bool(turned.get("ok")),
        # THE claim. A stream that was rebuilt is a pipeline that went down and came
        # back, whatever the session id says.
        "stream_rebuilt": before_token != after_token or after_token == 0,
        "frames_before": sum(1 for f in frames if at and f.at < at),
        "frames_after": sum(1 for f in frames if at and f.at >= at),
    }
    if "refused" in turned:
        out["refused"] = turned["refused"][:200]
    if gaps:
        widest, when = max(gaps)
        ordered = sorted(g for g, _ in gaps)
        out["worst_gap_ms"] = round(widest * 1000.0, 1)
        out["median_gap_ms"] = round(ordered[len(ordered) // 2] * 1000.0, 1)
        # Where the worst gap fell relative to the retune. A worst gap that is NOT at
        # the retune is a probe reporting an unrelated hiccup, and reading it as the
        # cost of the retune is exactly the kind of mistake this file keeps making.
        out["worst_gap_at_retune"] = bool(at) and abs(when - at) < 1.0
    return out


def _listen_verdict(
    session: "listen.Session",
    frames: list["listen.Frame"],
    peaks_seen: list[float],
    clip_seen: list[float],
    rms_seen: list[float],
    elapsed: float,
) -> dict[str, Any]:
    """What the session says, as claims that can fail rather than a dump of numbers.

    Every finding names something an owner or a maintainer could act on. A probe that
    printed twelve numbers and no verdict would leave the reading of them to whoever
    remembered what each was supposed to be."""
    findings: list[str] = []
    loud = max(peaks_seen) if peaks_seen else 0.0
    clipped = max(clip_seen) if clip_seen else 0.0
    level = max(rms_seen) if rms_seen else 0.0
    out: dict[str, Any] = {
        "engine": session.engine,
        "frequency_hz": session.frequency_hz,
        "mode": session.mode,
        "seconds": elapsed,
        "audio_peak_max": round(loud, 4),
        "audio_peak_mean": round(sum(peaks_seen) / len(peaks_seen), 4) if peaks_seen else 0.0,
        "audio_rms_max": round(level, 4),
        # What the AGC is doing, beside the level it is NOT allowed to move. AM and SSB
        # only; zero on FM (see `demod.AGC_WINDOW_S`). A quiet `audio_rms_max` next to
        # +34 dB here says "the station is weak and the owner can still hear it", which
        # neither number says alone.
        "agc_gain_db": round(session.audio_gain_db, 1),
        "clipped_fraction_max": round(clipped, 5),
        "overflows": session.overflows,
        "frames": len(frames),
        "fps": round(len(frames) / elapsed, 2) if elapsed > 0 else 0.0,
    }
    if session.engine != "iq":
        findings.append(
            f"the {session.engine} engine is running, so this radio could not be opened "
            f"for its own samples — there is no tuning view and the audio is a subprocess"
        )
    if loud <= 0.001:
        findings.append("no audio came out at all: the chain ran and produced silence")
    elif clipped >= CLIPPING_FRACTION:
        # The FRACTION that hit the rail, not the peak. This asked `loud >= 0.999`
        # first, which on FM is not a clipping test at all: a discriminator turns every
        # burst of noise that momentarily overpowers the carrier into a full-scale
        # impulse — the click a weak FM signal makes — so one sample in 1600 pins the
        # peak while the rest is a good voice. Measured on air, that read "clipping" on
        # NOAA weather at 17 dB SNR with 0.06% of samples actually at the rail.
        findings.append(
            f"{clipped * 100:.1f}% of samples hit the rail — the demodulator is "
            f"clipping, which on FM means the deviation scaling is too high here"
        )
    if session.overflows:
        findings.append(
            f"{session.overflows} USB buffers were dropped in {elapsed} s: the samples "
            f"either side of each are not adjacent in time, which is an audible click"
        )
    if session.engine == "iq" and not frames:
        findings.append("the engine ran but published no tuning rows")
    latest = frames[-1] if frames else None
    if latest is not None:
        span = latest.stop_hz - latest.start_hz
        middle = latest.start_hz + span / 2
        offset, loudest = _channel_centre(latest, middle)
        out["view"] = {
            "span_hz": round(span, 1),
            "passband_hz": latest.passband_hz,
            "bins": len(latest.db),
            "bin_hz": latest.bin_hz,
            "centre_hz": round(middle, 1),
            "strongest_offset_hz": round(offset, 1),
            "strongest_db": round(latest.db[loudest], 1),
            "floor_db": round(_channel_floor(latest), 1),
            "snr_db": round(latest.db[loudest] - _channel_floor(latest), 1),
        }
        snr = out["view"]["snr_db"]
        # THE FINDING THIS PROBE DID NOT HAVE, and the reason it passed a dead channel.
        # Every other check here is satisfied by pure noise: the chain runs, audio comes
        # out, the discriminator does not clip much, rows flow at 10 fps and the loudest
        # bin sits near the centre — because on FM, noise IS a full-scale signal. On air
        # 2026-09-06 that returned `ok: True` on 162.550 with nothing transmitting, and
        # the owner spent an evening looking for a demodulator fault. Signal strength is
        # the ONE question audio level cannot answer and the spectrum can.
        if snr < CHANNEL_SIGNAL_DB:
            findings.append(
                f"nothing is transmitting here: the strongest bin is {snr:.1f} dB over "
                f"the channel's own noise floor, so what the audio carries is hiss"
            )
        elif session.mode in ("fm", "nfm", "wbfm") and snr < FM_THRESHOLD_DB:
            findings.append(
                f"{snr:.1f} dB over the noise is below the FM threshold: there is a "
                f"signal here but it will be more click than voice"
            )
        if abs(middle - session.frequency_hz) > latest.bin_hz:
            # The offset-tuning failure, and the reason this number is reported rather
            # than assumed: the radio sits above the station and the mixer takes that
            # back out, so a row centred anywhere else means the two disagree.
            findings.append(
                f"the row is centred on {middle:.0f} Hz but the radio is tuned to "
                f"{session.frequency_hz} — the mixer and the tuning disagree"
            )
        if not latest.passband_hz:
            findings.append("the rows carry no passband, so the PWA will not draw them")
    out["findings"] = findings
    out["ok"] = not findings
    out["summary"] = (
        f"{session.engine} engine, {out['fps']} fps, peak {out['audio_peak_max']}"
        if not findings
        else findings[0]
    )
    return out


#: A bin this far below the row's own strongest reading is not a signal in that row.
#: Only used to decide whether a station SEEN in one row is still there in the next,
#: which is a question about presence rather than about level.
STEADY_MARGIN_DB = 12.0


def _steadiness(frames: list["listen.Frame"]) -> dict[str, Any]:
    """Does the picture HOLD STILL, row to row — and is any of it missing?

    The question the owner kept asking and nothing could answer: "the spectrum is
    intermittent... it's not a continuous bar, it has like little blank sections in it".
    A waterfall is a picture of time, so an intermittent one is either rows that lost
    bins, or a station that is measured in some rows and not others. Those have
    completely different causes and the same appearance, and guessing between them from
    a photograph has already cost one wrong fix (the hop dwell) and one fix aimed at the
    renderer. So it is measured here, on the box, where the rows are.

    **`gaps`** counts bins no measurement reached. `peaks.find` skips them and the PWA
    paints them transparent, both deliberately — a bin that measured nothing is not a
    quiet bin — so a row that lost a hop draws as a hole rather than as a floor.

    **`carrier_seen_in`** takes the strongest bin of the LAST row and asks how many rows
    had something within `STEADY_MARGIN_DB` of their own strongest at that same bin. An
    FM carrier is on continuously, so anything short of every row is the measurement
    blinking, not the station.
    """
    rows = len(frames)
    gaps = 0
    for frame in frames:
        gaps += sum(1 for value in frame.db if not math.isfinite(value))
    last = frames[-1]
    seen = 0
    swing = 0.0
    at = -1
    if last.db:
        finite = [(value, index) for index, value in enumerate(last.db) if math.isfinite(value)]
        if finite:
            _best, at = max(finite)
            levels: list[float] = []
            for frame in frames:
                if at >= len(frame.db):
                    continue
                value = frame.db[at]
                row_best = max((v for v in frame.db if math.isfinite(v)), default=math.nan)
                if not math.isfinite(value) or not math.isfinite(row_best):
                    continue
                levels.append(value)
                if value >= row_best - STEADY_MARGIN_DB:
                    seen += 1
            if levels:
                swing = round(max(levels) - min(levels), 1)
    return {
        "rows": rows,
        # Bins no measurement reached, over every row — a hole in the picture.
        "gaps": gaps,
        "gaps_per_row": round(gaps / rows, 1) if rows else 0.0,
        "carrier_bin": at,
        # How many rows still had the strongest carrier standing up in them.
        "carrier_seen_in": seen,
        # How far its level moved across the watch. A wideband-FM carrier sweeps its own
        # deviation, so a short look at one BIN of it is a look at the modulation.
        "carrier_swing_db": swing,
    }


#: How little headroom is worth saying something about, in dB below full scale.
#:
#: 6 dB is one bit of an eight-bit converter. Above it a band that doubles in level
#: after dark still has somewhere to go; under it the next loud thing clips, and on the
#: HF path nothing in software can prevent that — which is why it is said EARLY rather
#: than reported once the damage is in the picture.
LOW_HEADROOM_DB = 6.0

#: What share of samples has to reach the rail before it is CLIPPING rather than an
#: impulse, as a fraction.
#:
#: MEASURED 2026-09-07 on the first real use of this check: CB at 27 MHz came back with
#: `clipped_share: 2e-05` — two samples in a hundred thousand — and the finding called
#: it overload. That is not overload, it is lightning, an ignition spark or a switching
#: transient, and on a receiver with no AGC there is nothing to do about a single
#: impulse and nothing wrong with one. 1e-4 is a sample in ten thousand, which at
#: 2.4 MS/s is 240 a second: no impulse source produces that, and a front end being
#: driven into its rails produces far more. The SHARE is still reported under `level`
#: either way — the number is worth seeing; it just does not raise an alarm on its own.
CLIPPING_SHARE = 1e-4


def _level_fix(sweep: listen.Sweep) -> str:
    """What to actually do about a capture that is too hot, for THIS band.

    The two signal paths have different answers and the first version of this gave the
    HF one to both — measured on CB at 27 MHz, which is the tuner path, where the fix is
    simply a lower gain. Below 24 MHz the tuner is powered down and there is no gain
    stage at all, so nothing in software can help and the change has to be physical."""
    if sweep.stop_hz <= radio.DIRECT_MAX_HZ:
        return (
            "below 24 MHz the tuner is powered down and there is no gain to turn "
            "down, so the fix is physical — an attenuator, or a filter for whatever "
            "is loudest (usually the AM broadcast band)"
        )
    return (
        "this band is on the tuner, so the first thing to try is a lower gain "
        f"(`--gain`, currently {listen.MEASURING_GAIN_DB:g} dB for a measurement)"
    )


def _spectrum_verdict(
    sweep: listen.Sweep, frames: list[listen.Frame], elapsed: float, engine: str
) -> dict[str, Any]:
    """What the frames say, as claims that can fail rather than a dump of numbers."""
    findings: list[str] = []
    out: dict[str, Any] = {
        "engine": engine,
        "requested": sweep.as_dict(),
        "frames": len(frames),
        "elapsed_s": elapsed,
        "fps": round(len(frames) / elapsed, 2) if elapsed > 0 else 0.0,
    }
    if not frames:
        out["ok"] = False
        out["summary"] = f"the {engine} engine produced no frames in {elapsed}s"
        out["findings"] = [
            "No waterfall row arrived at all — the picture an owner would see is blank."
        ]
        return out
    last = frames[-1]
    out["frame"] = {
        "start_hz": last.start_hz,
        "stop_hz": last.stop_hz,
        "bin_hz": last.bin_hz,
        "bins": len(last.db),
    }
    # What the rows actually FOUND, which nothing else can answer: the peaks ride on
    # every frame to the picture and to the agent, and until this was here the only way
    # to know whether a live band was producing any was to open the PWA and look — the
    # exact bind this probe exists to undo (CLAUDE.md #10). The strongest few rather
    # than all of them: this is a check that the finding works, not a band survey.
    out["signals"] = {
        "in_last_frame": len(last.peaks),
        "strongest": last.peaks[:5],
        # Over the whole watch, so a band whose traffic is intermittent is not judged
        # by whichever row the probe happened to stop on.
        "rows_with_any": sum(1 for frame in frames if frame.peaks),
    }
    out["steadiness"] = _steadiness(frames)
    if sweep.capture is not None:
        rate_hz, fft_bins = sweep.capture
        want = iq.bin_width_hz(rate_hz, fft_bins)
        out["frame"]["expected_bin_hz"] = want
        if last.bin_hz != want:
            findings.append(
                f"the frame declares {last.bin_hz} Hz bins and the capture produces "
                f"{want} — a width nothing computed, which nothing downstream can see"
            )
        # A hopped row is several captures wide, and only the TRUSTED MIDDLE of each
        # reaches it, so the count to check against is per-hop-usable times hops. The
        # flat comparison against `fft_bins` called every correct hopped frame a fault
        # — measured on fm-broadcast, 2026-09-05: "2332 bins against the 256 the
        # capture asks for", where 2332 is exactly 11 hops of 212 usable bins.
        #
        # ...and then CROPPED to the span that was asked for, because whole hops
        # overshoot it. Leaving that out made this cry wolf on every correct hopped
        # frame all over again — measured on fm-broadcast, 2026-09-07: "2134 bins
        # against the 2332 the capture asks for", where 2134 is exactly 88-108 MHz at
        # 9375 Hz. Twice now in the same check, in opposite directions: it has to
        # follow the whole arithmetic, not the half of it nearest to hand.
        expect = fft_bins
        if sweep.hops > 1:
            expect = min(
                listen.hop_usable_bins(fft_bins) * sweep.hops,
                math.ceil((sweep.stop_hz - sweep.start_hz) / want),
            )
        if len(last.db) != expect:
            findings.append(
                f"{len(last.db)} bins per frame against the {expect} the capture asks "
                f"for — the transform is not the one the api chose"
            )
        if engine != "iq":
            findings.append(
                f"a one-hop capture was named and the {engine} engine ran anyway — the "
                f"radio would not open, so this is the runtime fallback, not the engine"
            )
        elif out["fps"] <= RTL_POWER_CEILING_FPS:
            # WHAT THE DISCRIMINATOR MEANS, not "this equals the clamp". `rtl_power`'s
            # interval is clamped to >= 1 s in its own C, and `RTL_POWER_CEILING_FPS` is
            # 1.5 — a MARGIN over that, so a rate at or under it cannot tell the two
            # engines apart. This said "no better than rtl_power's own one-second clamp",
            # which at 1.5 fps is simply false: 1.5 is half again quicker than 1.0.
            # MEASURED after C29 the 30 MHz row runs at exactly 1.50, which is where a
            # sentence claiming equality with the clamp stopped being true.
            findings.append(
                f"{out['fps']} fps is inside the band where a frame rate cannot tell "
                f"this engine from rtl_power, whose interval is clamped to one second — "
                f"the picture is real, but its rate is no longer evidence of which "
                f"engine drew it"
            )
    # THE LEVEL THE ANTENNA DELIVERED, which every dB in `db` is blind to: the row is
    # normalised power per bin, so a capture pinned against the converter's rails and a
    # capture with 30 dB spare draw the same shape. It matters most exactly where this
    # box has no answer for it — below 24 MHz the tuner is powered down, so there is no
    # gain to trim and the fix is physical (an attenuator, or a filter for whatever is
    # loudest). Worst frame of the watch rather than the last: one overloaded row is a
    # row of intermodulation the peak finder reports as stations.
    levels = [f for f in frames if f.headroom_db is not None]
    if levels:
        tightest = min(levels, key=lambda f: f.headroom_db or 0.0)
        loudest = max(levels, key=lambda f: f.clipped_share)
        out["level"] = {
            "headroom_db": tightest.headroom_db,
            "clipped_share": round(loudest.clipped_share, 6),
            "rows": len(levels),
        }
        if loudest.clipped_share >= CLIPPING_SHARE:
            findings.append(
                f"the converter CLIPPED on {round(loudest.clipped_share * 100, 3)}% of "
                f"one row's samples — a clipped capture spreads the loudest signal "
                f"across the whole span as intermodulation, and the peak finder reports "
                f"that as stations; {_level_fix(sweep)}"
            )
        elif (tightest.headroom_db or 0.0) < LOW_HEADROOM_DB:
            findings.append(
                f"only {tightest.headroom_db} dB of headroom left at the converter — "
                f"nothing is clipping yet, but a band that gets louder after dark will"
            )
    finite = [v for v in last.db if v > iq.DB_FLOOR]
    if not finite:
        findings.append(
            "every bin is at the zero-magnitude floor — nothing is reaching the ADC, "
            "which is the antenna or the input rather than this engine"
        )
    else:
        ordered = sorted(finite)
        out["frame"]["floor_db"] = round(ordered[len(ordered) // 2], 1)
        out["frame"]["peak_db"] = round(ordered[-1], 1)
    out["findings"] = findings
    out["ok"] = not findings
    out["summary"] = (
        f"{engine} engine, {len(frames)} frames in {elapsed}s ({out['fps']} fps), "
        f"{out['frame'].get('bins')} bins of {last.bin_hz} Hz"
    )
    return out


def _bin_hz_of(body: dict[str, Any]) -> int | float:
    """The requested width, kept exact when it is not an integer.

    `int()` here would have rounded 585.9375 to 585 and made the frame declare a width
    the transform never used — invisibly, because nothing downstream can tell (§6.13).
    Every pairing in `bands.LIVE_CAPTURES` divides exactly, so this is the guard for
    the day one does not rather than a case that happens today."""
    want = body.get("bin_hz", 25_000)
    return int(want) if float(want).is_integer() else float(want)


def _capture_of(body: dict[str, Any]) -> tuple[int, int] | None:
    """The one-hop capture the api chose, or None to leave this to rtl_power.

    Both halves or neither: a rate with no bin count cannot size a transform, and a bin
    count with no rate cannot place one. A caller that sends half of it gets the
    rtl_power path, which is the same answer it got before F6 rather than a failure."""
    rate, bins = body.get("rate_hz"), body.get("bins")
    if rate is None or bins is None:
        return None
    rate, bins = int(rate), int(bins)
    if rate <= 0 or bins <= 0:
        return None
    return rate, bins


def _hops_of(body: dict[str, Any]) -> int:
    """How many captures the span takes. Absent means one, which is what every caller
    before F11 meant — and reading it as more would sweep a band nobody asked for."""
    try:
        return max(1, int(body.get("hops") or 1))
    except (TypeError, ValueError):
        return 1


def _channel_hz_of(body: dict[str, Any]) -> int:
    """One channel's width on this band, from the api's band plan (`bands.py`).

    Absent means the caller did not say, and `peaks.find` then treats only touching bins
    as one signal — the honest answer without a band plan, and the one every caller
    before this meant."""
    try:
        return max(0, int(body.get("channel_hz") or 0))
    except (TypeError, ValueError):
        return 0


def _range_of(body: dict[str, Any]) -> listen.Sweep:
    """The span a sweeping request is asking for, bounds and all.

    Shared by the one-shot `/sweep` and by starting or moving a live spectrum, so the
    three cannot drift apart on what a legal range is — the tuner's limits are a fact
    about the radio, not about which route asked.

    ONE FLOOR since B2, which is the radio's own. It used to depend on which route
    asked — a fact about the ENGINE rather than about the radio, since `rtl_power`
    hardcoded the wrong ADC branch — and with one engine there is nothing left to
    select (`listen.Sweep.of`).

    `seconds` is how long a SURVEY runs and means nothing to a live spectrum, which
    runs until it is released. It is parsed either way rather than made conditional:
    `Sweep` is one type, and a field a caller may ignore is cheaper than two types that
    agree about everything else."""
    sweep = listen.Sweep.of(
        start_hz=int(body.get("start_hz", 0)),
        stop_hz=int(body.get("stop_hz", 0)),
        bin_hz=_bin_hz_of(body),
        seconds=float(body.get("seconds", 60)),
        capture=_capture_of(body),
        hops=_hops_of(body),
        channel_hz=_channel_hz_of(body),
    )
    floor = listen.DIRECT_MIN_HZ
    if not (floor <= sweep.start_hz and sweep.stop_hz <= MAX_HZ):
        raise ListenError(
            f"{sweep.start_hz}-{sweep.stop_hz} Hz is outside the radio's range "
            f"({floor}-{MAX_HZ} Hz)"
        )
    return sweep


def _peak(pcm: bytes) -> float:
    """Loudest sample of the DEMODULATED AUDIO, as a 0..1 fraction of full scale.

    **Not a signal level** (F9). It is measured after `rtl_fm`'s discriminator, AGC,
    squelch and de-emphasis, so it is not comparable with the spectrum path's true
    dBFS — nor even across the radio's own two signal paths, since idle airband AM
    reads 0.21 off the tuner's AGC amplifying its own noise while HF has no gain stage
    to do that. The wire field is `audio_peak` for exactly that reason.

    The cheapest honest answer to "is anything actually coming out of the radio?" —
    a tuned-but-silent capture and a capture of a dead port look identical in a WAV
    duration, and this tells them apart without a spectrum analyser.
    """
    if not pcm:
        return 0.0
    count = len(pcm) // 2
    peak = max(abs(v) for v in struct.unpack(f"<{count}h", pcm[: count * 2]))
    return round(peak / 32768.0, 4)


def capture(
    freq_hz: int, seconds: float, mode: str, gain: str | None, serial: str | None
) -> dict[str, Any]:
    """Tune, record `seconds` of audio, return a WAV plus what was heard.

    Reserves the radio for the whole capture and refuses rather than queues: a caller
    waiting an unknown time on a radio someone else is using is worse than a caller
    told plainly that it is busy."""
    if not listen.DIRECT_MIN_HZ <= freq_hz <= MAX_HZ:
        raise SdrError(
            f"{freq_hz} Hz is outside the tuner's range ({MIN_HZ}-{MAX_HZ} Hz)"
        )
    # A capture is meant to be a sample of what a session would hear, so it has to
    # refuse what a session refuses: between 14.4 and 24 MHz `demod_args` bypasses the
    # tuner and the request folds onto the first Nyquist zone. Shared with `listen`
    # rather than restated, for the reason `demod_args` itself is shared — the two
    # drifting apart is how a capture comes back sounding unlike the live audio.
    aliased = listen.aliased_refusal(freq_hz)
    if aliased is not None:
        raise SdrError(aliased)
    key = mode.lower()
    if key not in MODES:
        raise SdrError(f"unknown mode {mode!r} (want one of {sorted(MODES)})")
    seconds = max(0.5, min(float(seconds), MAX_SECONDS))

    # Per RADIO, not per box: a capture on one dongle is no reason to refuse one on
    # another. An unnamed capture still collides with everything, because it opens
    # whatever librtlsdr enumerates first (see `listen.blocking_key`).
    busy = TUNER.reserve(serial, "recording")
    if busy is not None:
        raise SdrBusy(str(busy))
    try:
        rate = NARROW_RATE
        cmd = ["rtl_fm", "-f", str(freq_hz), "-M", MODES[key]]
        if serial:
            # The BARE serial: librtlsdr's verbose_device_search has no key=value form,
            # so `serial=X` matches nothing and rtl_fm exits before opening the device.
            cmd += ["-d", str(serial)]
        # Shared with the live path rather than rebuilt here — see `listen.demod_args`.
        # A capture is meant to be a sample of what a session would hear, and it stops
        # being one the moment the two lists can drift apart.
        cmd += listen.demod_args(key, gain, freq_hz)
        cmd += ["-"]

        # rtl_fm streams until stopped, so the timeout IS the recording length and the
        # timeout branch is the NORMAL path, taken by every capture that records
        # anything at all.
        #
        # Which is why this cannot be `subprocess.run(timeout=...)`: on timeout CPython
        # calls `process.kill()` — SIGKILL — and `listen.Session._kill` exists to spell
        # out what that does to this hardware. rtl_fm installs a handler that cancels
        # the pending async USB transfer and CLOSES THE DEVICE; SIGKILL never runs it,
        # the RTL2832U is left with transfers submitted, and a dongle torn down that way
        # can stop answering the descriptor reads librtlsdr enumerates with — after
        # which every `-d <serial>` lookup fails and the radio reads as absent while
        # sysfs still lists it. The live path has been careful about this since it was
        # written; capture was quietly doing the opposite on every single run.
        #
        # So: SIGTERM, let the handler close the device, and keep SIGKILL for a tool
        # that has genuinely wedged. `communicate` is resumed rather than restarted —
        # it keeps what it has already read, which is the recording.
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            pcm, err = proc.communicate(timeout=seconds)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                pcm, err = proc.communicate(timeout=_SHUTDOWN_GRACE_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                pcm, err = proc.communicate()

        text = err.decode("utf-8", "replace")
        if not pcm:
            raise SdrError(
                f"rtl_fm produced no audio: {text.strip()[-400:] or 'no output'}"
            )

        return {
            "frequency_hz": freq_hz,
            "mode": key,
            "seconds": round(len(pcm) / 2 / rate, 2),
            "sample_rate": rate,
            "audio_peak": _peak(pcm),
            "device_log": text.strip()[-2000:],
            "wav": _wav(pcm, rate),
        }
    finally:
        TUNER.unreserve(serial)


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, body: dict[str, Any]) -> None:
        # Every refusal goes to the log as well as down the wire, because MEASURED
        # 2026-09-05 the wire is not enough: a 502 from here reached the owner's console
        # as the edge's own error page with `detail` stripped, so the reason existed
        # only in a response nobody could read. 4xx survives that trip and 5xx does not
        # (docs/runbooks/DEBUG_ACCESS.md), and the container log survives both — it is
        # the one channel `logs sdr` can always reach.
        if code >= 400:
            said = body.get("detail") or body.get("summary") or body
            print(f"[sdr] {code} {self.path.split('?')[0]}: {said}", flush=True)  # noqa: T201
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
        route = self.path.split("?")[0]
        if route == "/listen/audio":
            self._stream_audio()
            return
        if route == "/listen/segments":
            self._stream_segments()
            return
        if route == "/listen/packets":
            self._stream_packets()
            return
        if route == "/listen/spectrum":
            self._stream_spectrum()
            return
        if route != "/healthz":
            self._json(404, {"detail": "not found"})
            return
        # Report whether the tools are even present: a sidecar that starts but has no
        # rtl_fm is a failure the api should see at /healthz, not on first capture.
        # ONE reading for both fields below: taken separately, a session could appear in
        # `sessions` but not in `listening`, and the api's fallback assumes otherwise.
        session, live = TUNER.snapshot()
        self._json(
            200,
            {
                "status": "ok",
                "rtl_fm": shutil.which("rtl_fm") is not None,
                "ffmpeg": shutil.which("ffmpeg") is not None,
                "busy": TUNER.reserved(),
                # What jobs this sidecar knows how to hold the tuner for. Advertised
                # because an OLDER sidecar ignores an unknown `purpose` and returns 200
                # with a plain listening session — so "turn APRS logging on" against a
                # box that has not been updated would succeed, log nothing, and report
                # success. A caller can check here instead of trusting a 200.
                "purposes": list(PURPOSES),
                # ONE session, for the omnibox, which draws one icon. Kept as-is: the
                # api's SdrStatusOut.listening is what the PWA reads, and reshaping it
                # is a GUI change, not a sidecar one.
                "listening": session.info().as_dict() if session is not None else None,
                # ...and the whole truth beside it, because with a radio each for APRS
                # and the tuner, "the session" is no longer a thing that exists. A
                # caller that asks `listening` whether APRS is running now gets the
                # right answer only when nothing else is holding a radio.
                "sessions": [s.info().as_dict() for s in live],
            },
        )

    def _body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            return cast(dict[str, Any], json.loads(self.rfile.read(length) or b"{}"))
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"detail": "body must be JSON"})
            return None

    def _stream_audio(self) -> None:
        """Stream the live session's MP3 to one listener until they hang up.

        No Content-Length: this is an open-ended chunked response, which is what an
        `<audio>` element wants from a live source. A listener can arrive at ANY point
        in the session — the tuner sheet opens and closes many times over one lease —
        and MP3's self-framing is what makes that work (deploy/sdr/listen.py)."""
        # The LISTENING session specifically. With several radios in use "the session"
        # is no longer one thing, and streaming an APRS lease's audio to the tuner sheet
        # would hand the owner 1200-baud AFSK where they expected a voice.
        session = TUNER.for_purpose(PURPOSE_LISTEN)
        if session is None:
            self._json(409, {"detail": "nothing is listening"})
            return
        sub = session.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", AUDIO_CONTENT_TYPE)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                chunk = sub.get()
                if chunk is None:  # the session ended
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # the listener closed the tab; entirely normal
        finally:
            session.unsubscribe(sub)

    def _stream_segments(self) -> None:
        """Hand one captioner a stream of WAV segments, newline-framed.

        Each frame is a JSON header line then that many bytes of WAV, so a caller can
        read segments without a second connection or a polling loop. Segments are cut
        on quiet gaps and squelched at the source, so what arrives here is audio that
        actually had someone talking in it (deploy/sdr/listen.py).

        Framing rather than one-WAV-per-request because the interesting property is
        CONTINUITY: a captioner that has to re-request loses the audio between calls,
        and the gap always lands mid-sentence."""
        session = TUNER.for_purpose(PURPOSE_LISTEN)
        if session is None:
            self._json(409, {"detail": "nothing is listening"})
            return
        sub = session.subscribe_segments()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                try:
                    started, pcm = sub.get(timeout=30)
                except queue.Empty:
                    # A keep-alive: a silent channel must not look like a dead socket.
                    self.wfile.write(b'{"keepalive":true}\n')
                    self.wfile.flush()
                    continue
                wav = _wav(pcm, AUDIO_RATE)
                head = json.dumps(
                    {
                        "started_at": started,
                        "bytes": len(wav),
                        "seconds": round(len(pcm) / 2 / AUDIO_RATE, 2),
                    }
                )
                self.wfile.write(head.encode() + b"\n")
                self.wfile.write(wav)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # the captioner hung up; entirely normal
        finally:
            session.unsubscribe_segments(sub)

    def _stream_packets(self) -> None:
        """Hand one reader a stream of decoded APRS frames, newline-framed JSON.

        Same shape as `/listen/segments`: one JSON object per line, plus keep-alives
        so a quiet channel does not look like a dead socket. A quiet channel is the
        NORMAL case here — a packet frequency can go minutes between frames — which is
        why the keep-alive matters more on this route than on the audio one."""
        # Ask for the APRS session by name. Reading "the" session and then checking its
        # purpose answered "nothing is logging" whenever the tuner happened to hold a
        # DIFFERENT radio — which is now an ordinary state rather than an impossible one.
        session = TUNER.for_purpose(PURPOSE_APRS)
        if session is None:
            session = TUNER.current()
        if session is None:
            self._json(409, {"detail": "the radio is idle — nothing is logging APRS"})
            return
        if session.purpose != PURPOSE_APRS:
            # Name the holder, for the reason P0 exists: "not logging APRS" is true of
            # an idle radio and of one busy listening, and those need opposite answers.
            held = PURPOSE_LABEL.get(session.purpose, "in use")
            self._json(409, {"detail": f"the radio is {held}, not logging APRS"})
            return
        sub = session.subscribe_packets()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                try:
                    packet = sub.get(timeout=20)
                except queue.Empty:
                    self.wfile.write(b'{"keepalive":true}\n')
                    self.wfile.flush()
                    continue
                if packet is None:
                    return  # the session ended; the stream ends with it
                row = packet.as_dict()
                row["frequency_hz"] = session.frequency_hz
                self.wfile.write(json.dumps(row).encode() + b"\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # the reader hung up; entirely normal
        finally:
            session.unsubscribe_packets(sub)

    def _view_wanted(self) -> str | None:
        """The `?view=` asked for: a name, "" for "the session decides", None for junk.

        Validated rather than coerced, because silently serving the band to a viewer that
        asked for the channel is exactly the class of quiet substitution B1 exists to
        stop — a picture that is not of what it says it is."""
        query = urllib.parse.urlsplit(self.path).query
        asked = urllib.parse.parse_qs(query).get("view", [""])[0].strip().lower()
        if not asked:
            return ""
        return asked if asked in listen.VIEWS else None

    def _stream_spectrum(self) -> None:
        """Hand one viewer a stream of waterfall rows, newline-framed JSON.

        Same shape as `/listen/packets`, and for the same two reasons: one JSON object
        per line so a reader needs no framing of its own, and keep-alives so a stream
        that is between frames does not look like a dead socket.

        Each row carries its own range (`listen.Frame`), so the api relays lines without
        understanding them and a retune needs no message of its own."""
        # Whichever session is DRAWING, not only a spectrum one: a listening session on
        # the I/Q engine publishes the tuning view of the channel it is demodulating
        # through this same seam, because it is the same `Frame` measured off the same
        # samples. Each row says which band it covers, so a reader needs no warning
        # that it is now looking at 32 kHz of one channel rather than 20 MHz of a dial.
        # WHICH picture, on a session that now draws two off one capture. Absent means
        # the session's own default — what its rows meant before there was more than one
        # kind — so a client that never learns about views sees no change at all.
        wanted = self._view_wanted()
        if wanted is None:
            self._json(
                400,
                {"detail": f"view must be one of {', '.join(listen.VIEWS)}"},
            )
            return
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        named = (query.get("serial", [""])[0] or "").strip() or None
        try:
            backfill = int(query.get("backfill", ["1"])[0])
        except ValueError:
            backfill = 1
        # WHICH RADIO, and this is the whole of the bug it fixes. `drawing()` prefers a
        # spectrum session, so on a two-dongle box with a spectrum running, a viewer
        # asking for the TUNING strip of a listening radio was attached to the other
        # radio's band picture and waited for channel rows that could never come. It
        # read as "waiting for the radio" under audio that was plainly playing.
        session = TUNER.drawing(serial=named)
        if session is None:
            held = TUNER.sessions()
            if named and held:
                self._json(
                    409,
                    {"detail": f"radio {named} is not drawing anything"},
                )
                return
            if held:
                # Name the holder rather than saying "idle", which is false and sends
                # the owner looking for a radio that is plainly in use.
                doing = PURPOSE_LABEL.get(held[0].purpose, "in use")
                detail = f"the radio is {doing} and is not drawing anything"
                self._json(409, {"detail": detail})
                return
            self._json(409, {"detail": "nothing is watching the spectrum"})
            return
        sub, seeded = session.subscribe_frames(wanted or None, backfill=backfill)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            # The rows already drawn, oldest first, before a single live one — so a
            # viewer that comes back to a picture that has been running for minutes
            # gets the picture rather than a blank box filling at two rows a second.
            for frame in seeded:
                self.wfile.write(json.dumps(frame.as_dict()).encode() + b"\n")
            self.wfile.flush()
            while True:
                try:
                    frame = sub.get(timeout=20)
                except queue.Empty:
                    self.wfile.write(b'{"keepalive":true}\n')
                    self.wfile.flush()
                    continue
                if frame is None:
                    return  # the session ended; the stream ends with it
                self.wfile.write(json.dumps(frame.as_dict()).encode() + b"\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # the viewer hung up; entirely normal
        finally:
            session.unsubscribe_frames(sub)

    def _sweep(self) -> None:
        """Run one band survey and return the CSV the api reduces.

        **The survey is an accumulator over a live spectrum now, not a second engine**
        (`docs/archive/SDR_RECEIVER_CONVERGENCE_PLAN.md` A5/B2). It was a fourth session
        kind with its own lifecycle, its own temp CSV and a handler that pinned a thread
        for up to fifteen minutes — strictly LESS capable than the picture it duplicated,
        because `rtl_power -D` hardcodes the ADC branch this board does not wire and so a
        survey could never reach shortwave. It can now, on the same engine the waterfall
        uses, and the CSV that comes out is the shape `sdr/sweep.py` already parses.

        The sidecar's job here is still the RADIO: hold the lease, integrate, hand back
        the numbers. It does not reduce them and it does not draw them — a plotting stack
        is what `Dockerfile.sdr`'s apt-only rule refuses, and the api already carries
        Pillow. Sending the raw CSV also means a better reduction can be run over an old
        survey later, the same reasoning that keeps `raw` on every APRS row.

        Synchronous: the request is held for the length of the survey. That is the
        caller's problem to solve (the debug route runs it as a background job), not a
        reason for the sidecar to grow a job table of its own."""
        body = self._body()
        if body is None:
            return
        try:
            # `direct_ok`: the floor that kept surveys above 24 MHz was rtl_power's, and
            # rtl_power is gone. What replaces it is the same rule a picture gets — a
            # range with no capture plan is refused in words.
            sweep = _range_of(body)
        except (TypeError, ValueError) as bad:
            self._json(400, {"detail": f"a sweep needs numbers: {bad}"})
            return
        except ListenError as refused:
            self._json(400, {"detail": str(refused)})
            return
        unservable = listen.spectrum_engine_refusal(sweep)
        if unservable is not None:
            self._json(400, {"detail": unservable})
            return
        # The width the survey is WRITTEN at, which is the caller's to choose and is not
        # `sweep.bin_hz` — that is the width the transform runs at, and a survey asks to
        # be integrated more coarsely than it is measured. `SurveyRows` folds one into
        # the other and states on every line which it used.
        want_bin_hz = _bin_hz_of(body)

        try:
            info = TUNER.start(
                sweep.centre_hz,
                "fm",
                body.get("gain"),
                purpose=PURPOSE_SPECTRUM,
                sweep=sweep,
                serial=listen.validate_serial(body.get("serial")),
            )
        except ListenBusy as busy:
            self._json(409, {"detail": str(busy)})
            return
        except ListenError as bad:
            self._json(400, {"detail": str(bad)})
            return

        session = TUNER.find(info.session_id)
        if session is None:
            self._json(502, {"detail": "the survey session was gone before it started"})
            return
        rows = listen.SurveyRows(want_bin_hz)
        lines: list[str] = []
        sub, _ = session.subscribe_frames(listen.VIEW_BAND)
        started = time.monotonic()
        deadline = started + sweep.seconds
        stopped_early = False
        try:
            while time.monotonic() < deadline:
                try:
                    frame = sub.get(timeout=max(0.1, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if frame is None:
                    # The radio went away mid-survey. What has been measured so far is
                    # still a real measurement of a shorter window.
                    stopped_early = True
                    break
                lines.extend(rows.push(frame))
        finally:
            session.unsubscribe_frames(sub)
            with contextlib.suppress(Exception):
                # Released HERE rather than by the caller: a survey that walked away
                # holding the radio is one the owner has no terminal to free.
                TUNER.stop(info.session_id)
        # The open interval, which is a real measurement of a shorter one. Dropping it
        # would silently shorten every survey by up to `SURVEY_INTERVAL_S`.
        lines.extend(rows.flush())
        csv_text = "\n".join(lines)

        if not csv_text.strip():
            # A survey that measured NOTHING is a failure, not a quiet band, and it used
            # to answer `complete: true` with an empty CSV — a success over a measurement
            # that never happened. The session's own last words go back with the refusal,
            # because the owner cannot read the container log (CLAUDE.md #10).
            said = (session.refusal or session.tail) if session else ""
            self._json(
                502,
                {"detail": f"the sweep measured nothing: {said or 'no rows were measured'}"},
            )
            return

        self._json(
            200,
            {
                "start_hz": sweep.start_hz,
                "stop_hz": sweep.stop_hz,
                # What the ROWS say, which is what the reducer reads. NOT the width
                # that was asked for: folding is by whole capture bins, so 25 kHz off a
                # 4687.5 Hz capture is five of them and the rows say 23437.5. An
                # envelope claiming otherwise would be the same lie as a frame declaring
                # a width nothing computed, one layer up.
                "bin_hz": rows.step_hz or want_bin_hz,
                "seconds": round(time.monotonic() - started, 2),
                "gain": body.get("gain"),
                # A survey that hit the deadline still returns its rows. A partial survey
                # is a real measurement of a shorter window, and throwing it away would
                # cost the caller the whole run.
                "complete": not stopped_early,
                "csv": csv_text,
            },
        )

    def _reset(self, body: dict[str, Any]) -> None:
        """Re-enumerate one radio, for a dongle that has stopped answering.

        THE ONLY RECOVERY that does not involve hands. An RTL-SDR left with transfers
        pending — an unclean teardown, a brown-out — can stay on the bus and stop
        answering descriptor reads, after which every `-d <serial>` lookup fails and the
        radio looks absent while sysfs still lists it. A container restart does not clear
        that; a rebuild does not either; only a port reset or a person does. The owner
        runs this box remotely with no terminal (CLAUDE.md #10), so "unplug it" is not an
        answer, and this is the thing that stops it being one.

        The node comes from the api, resolved through the supervisor's sysfs scan,
        because resolving a serial to a node is precisely what a broken device cannot
        help with — and sysfs answers it anyway from what the kernel cached when the
        device first enumerated.

        Through the LEASE, so a reset cannot be run under a session that is using the
        radio: `reserve` refuses if it is held and holds it against anything starting
        mid-reset. The other radio is untouched — this resets one device, so APRS on a
        second dongle keeps logging."""
        serial = body.get("serial")
        node = str(body.get("device_node") or "")
        try:
            named = listen.validate_serial(serial)
        except ListenError as bad:
            self._json(400, {"detail": str(bad)})
            return
        busy = TUNER.reserve(named, "resetting the radio")
        if busy is not None:
            self._json(409, {"detail": str(busy)})
            return
        # **Decide while the lease is held, release it, and only then answer.** Writing
        # the response from inside the `try` loses a race the caller can never win: the
        # client has the reply in hand while this thread has not yet reached its
        # `finally`, and the very next thing anything does after a reset is try to use
        # the radio — which the lease is still refusing, with "the radio is already
        # resetting the radio". Invisible on an idle box and reproducible on a busy one,
        # which is the worst way for a recovery path to fail: it fails hardest exactly
        # when it is most needed, and it reads to the owner as "the reset did not work".
        answer: tuple[int, dict[str, Any]]
        try:
            # The lease is not the whole answer any more. It knows about child processes
            # and TTL reservations; it cannot see an IN-PROCESS device handle, and the
            # leak case is exactly the dangerous one — the lease believes the radio is
            # free (that is what leaked means), so the ioctl would fire from the very
            # process still holding the usbfs fd with interface 0 claimed. The device
            # then re-enumerates at a new node, the orphaned libusb handle goes ENODEV
            # and is never closed, and nothing in `radio.py` learns. Asked through the
            # SAME `blocking_key` the lease uses, so an unnamed handle blocks every
            # radio here exactly as an unnamed session does there.
            open_here = listen.blocking_key(radio.holders(), named or listen.ANY_DEVICE)
            if open_here is not None:
                doing = radio.holders().get(open_here, "in use")
                answer = (
                    409,
                    {
                        "detail": f"the radio ({open_here or 'unnamed'}) is still open "
                        f"in this process ({doing}), and a port reset would strand that "
                        f"handle at a new node. Stop that job first — or, if nothing "
                        f"will, restart the sdr service."
                    },
                )
            else:
                usbdev.reset(node)
                # The node number changes as a result — a reset device comes back at the
                # next free address — so the caller is told to look again rather than
                # reuse it.
                answer = (200, {"reset": True, "serial": named, "was": node})
        except ValueError as bad:
            answer = (400, {"detail": str(bad)})
        except OSError as failed:
            # ENODEV is the ordinary one: the node moved between the scan and now, which
            # a reset itself causes. Said plainly, because "try again" really is the fix.
            answer = (
                502,
                {"detail": f"the radio would not reset ({failed.strerror or failed}). "},
            )
        finally:
            TUNER.unreserve(named)
        self._json(*answer)

    def _soapy_probe(self, body: dict[str, Any]) -> None:
        """F0's questions, asked of a real dongle, answered as a verdict.

        Everything `radio.py` is written against — enumeration, `serial=` selection,
        `direct_samp=2`, live retune and live rate change with no stream rebuild, a real
        `SOAPY_SDR_OVERFLOW` under induced backpressure, the achieved rate against the
        requested one, and whether `bufflen` actually took — is a claim read off source
        and datasheets. This is where hardware retires them, and the owner has no
        terminal to run `SoapySDRUtil` from (CLAUDE.md #10).

        THROUGH THE LEASE, like every other holder here, so a probe cannot run under a
        live session and a session cannot start under a probe. Released in a `finally`
        for the reason `capture` is: a claim staked and not returned refuses that radio
        until the TTL lapses."""
        try:
            named = listen.validate_serial(body.get("serial"))
            center_hz = int(body.get("center_hz") or radio.PROBE_CENTER_HZ)
            rate_hz = int(body.get("rate_hz") or radio.PROBE_RATE_HZ)
            bins = int(body.get("bins") or radio.PROBE_BINS)
        except (ListenError, TypeError, ValueError) as bad:
            self._json(400, {"detail": str(bad)})
            return
        if not listen.DIRECT_MIN_HZ <= center_hz <= MAX_HZ:
            self._json(
                400,
                {"detail": f"{center_hz} Hz is outside the radio's range "
                 f"({listen.DIRECT_MIN_HZ}-{MAX_HZ} Hz)"},
            )
            return
        # The legal rates are librtlsdr's, not ours: it rejects everything outside
        # 225001-300000 and 900001-3200000, and a probe asking for one gets the driver's
        # own refusal, which is more informative than a bound restated here. These two
        # only keep the request from allocating something absurd.
        if not 225_000 <= rate_hz <= 3_200_000:
            self._json(400, {"detail": f"{rate_hz} S/s is not a rate this radio takes"})
            return
        if not 16 <= bins <= 8_192:
            self._json(400, {"detail": f"{bins} bins is outside 16-8192"})
            return
        busy = TUNER.reserve(named, "probing the radio")
        if busy is not None:
            self._json(409, {"detail": str(busy)})
            return
        # The lease is given back BEFORE the response is written, not in a `finally`
        # after it. Sending first leaves a window in which the caller has a 200 in hand
        # and the radio is still reserved, so anything acting on that answer — a session,
        # a second probe — meets a 409 for a hold that is already over. `capture` has
        # always released before it answered; this did not, and the seam showed up as a
        # test that passed alone and failed in a full run.
        code: int = 200
        answer: dict[str, Any]
        try:
            answer = radio.probe(
                serial=named, center_hz=center_hz, rate_hz=rate_hz, bins=bins
            )
        except radio.RadioBusy as held:
            code, answer = 409, {"detail": str(held)}
        except radio.RadioError as failed:
            # 502 rather than 500: the radio answered badly (or not at all), which is
            # the same class of failure as a sidecar refusing a sweep, and the sentence
            # is the useful part.
            code, answer = 502, {"detail": str(failed)}
        except ValueError as bad:
            code, answer = 400, {"detail": str(bad)}
        finally:
            TUNER.unreserve(named)
        self._json(code, answer)

    def _spectrum_probe(self, body: dict[str, Any]) -> None:
        """Does the LIVE SPECTRUM really behave the way F6 claims, on this radio?

        `soapy/probe` retires the claims `radio.py` is written against; this retires the
        ones the ENGINE is, and there is no other way to ask. A live spectrum is an
        owner route behind a websocket, so before this the only way to see whether F6
        worked on real hardware was for the owner to open the Radio tab and look — and
        an owner with no terminal cannot be the test harness for their own box
        (CLAUDE.md #10). It answers three questions nothing else can:

        **Which engine actually ran.** The api names a one-hop capture and the sidecar
        drops to `rtl_power` at RUNTIME when a radio will not open, so the engine in use
        is a fact about this moment rather than about the request — and a silent
        downgrade is a waterfall quietly at a tenth of the frame rate it claims.

        **Whether `bin_hz` on the wire is exactly `rate / bins`.** The whole of F7 and
        the reason the width stopped being negotiated with a tool; a frame that declares
        a width the transform never used is the one failure nothing downstream can see.

        **What the frame rate really is.** `rtl_power` clamps its interval to one second
        in C, which is the ceiling this plan exists to remove. A measured rate above
        that IS the removal, and it cannot be inferred from anything static.

        Runs for a few seconds and RELEASES, through the same lease as everything
        else — so it is refused with a 409 naming the holder, and it never leaves a
        session behind on a box whose owner cannot go and stop one."""
        try:
            sweep = _range_of(body)
            seconds = float(body.get("seconds") or SPECTRUM_PROBE_S)
            named = listen.validate_serial(body.get("serial"))
        except (ListenError, TypeError, ValueError) as bad:
            self._json(400, {"detail": str(bad)})
            return
        seconds = max(1.0, min(seconds, SPECTRUM_PROBE_MAX_S))
        try:
            info = TUNER.start(
                sweep.centre_hz,
                "fm",
                body.get("gain"),
                purpose=PURPOSE_SPECTRUM,
                sweep=sweep,
                serial=named,
            )
        except SdrBusy as busy:
            self._json(409, {"detail": str(busy)})
            return
        except (SdrError, ValueError) as bad:
            self._json(400, {"detail": str(bad)})
            return
        try:
            answer = self._watch_spectrum(sweep, seconds, info.session_id)
        finally:
            # Released here, not by the caller: a probe that leaves the radio held is
            # worse than one that fails, because the owner has no terminal to free it
            # from and the next thing to ask for a radio meets a 409 about a probe that
            # already answered.
            with contextlib.suppress(Exception):
                TUNER.stop(info.session_id)
        self._json(200, answer)

    def _watch_spectrum(
        self, sweep: listen.Sweep, seconds: float, session_id: str
    ) -> dict[str, Any]:
        """Collect frames for `seconds` and reduce them to a verdict."""
        # BY ID. This read `TUNER.current()` and then checked the id, which on a
        # two-radio box answered with whichever session the sidecar ranked first — so a
        # probe running beside a listening session reported "the spectrum session was
        # gone" while it was measuring perfectly. Found by B7, which is what took that
        # ranking out of this process.
        session = TUNER.find(session_id)
        if session is None:
            return {
                "ok": False,
                "summary": "the spectrum session was gone before a frame arrived",
                "findings": ["nothing held the radio by the time the probe looked"],
            }
        # `session.engine`, not a guess from a private attribute: the session knows
        # which engine it started and now says so on every path.
        engine = session.engine
        # THE BAND, named rather than defaulted: this probe's verdict is about a wideband
        # picture, and a session that also drew a channel would otherwise get to answer
        # with rows measured over 32 kHz of one station.
        sub, _ = session.subscribe_frames(listen.VIEW_BAND)
        frames: list[listen.Frame] = []
        started = time.monotonic()
        deadline = started + seconds
        try:
            while time.monotonic() < deadline:
                try:
                    frame = sub.get(timeout=max(0.1, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if frame is None:
                    break
                frames.append(frame)
        finally:
            session.unsubscribe_frames(sub)
        elapsed = round(time.monotonic() - started, 2)
        return _spectrum_verdict(sweep, frames, elapsed, engine)


    def _listen_probe(self, body: dict[str, Any]) -> None:
        """Does the NUMPY DEMODULATOR really work on this radio?

        The twin of `_spectrum_probe`, one layer over: that one retires the claims the
        spectrum engine is written against, this retires the ones `demod.py` is. Both
        exist for the same reason — listening is an owner route and the audio is an MP3
        stream, so before this the only way to know whether the demodulator worked on
        real hardware was for the owner to press play and listen, and an owner with no
        terminal cannot be the test harness for their own box (CLAUDE.md #10).

        Four things it can catch, none of them visible anywhere else and none of them
        provable against the synthetic signals `test_sdr_demod.py` uses:

        **Which engine ran.** The listen pipeline drops to `rtl_fm` at RUNTIME when a
        radio will not open for our own samples. That fallback is right and its
        silence is not: on `rtl_fm` there is no tuning view at all, and nothing else
        says why.

        **Whether the station is where the offset says.** The radio is tuned
        `LISTEN_OFFSET_HZ` above it and the mixer takes that back out. Get the two out
        of step and a narrowband channel is silence — so the probe reports where the
        strongest bin actually landed, and a quarter of a megahertz of error is not
        subtle.

        **Whether the audio is a signal or a rail.** A peak pinned at 1.0 is a chain
        clipping, a peak at 0.0 is one not connected; both look like "it ran".

        **Whether the stream stays continuous.** `Reading.overflows` counts USB buffers
        the driver threw away, which on a waterfall is one row slightly wrong and on
        audio is an audible click. This is the one measurement a fake radio cannot
        produce, and the reason `QUEUE_BUFFERS` is worth re-examining now that the same
        stream has to run continuously rather than in frames.

        **TAKES A RADIO** for those seconds through the same lease as everything else,
        and releases it even when the probe fails."""
        try:
            mhz = float(body.get("mhz") or 0.0)
            mode = str(body.get("mode") or "fm")
            seconds = float(body.get("seconds") or LISTEN_PROBE_S)
            named = listen.validate_serial(body.get("serial"))
            want_audio = bool(body.get("audio"))
            want_band = bool(body.get("band"))
            retune_mhz = float(body.get("retune_mhz") or 0.0)
        except (ListenError, TypeError, ValueError) as bad:
            self._json(400, {"detail": str(bad)})
            return
        seconds = max(1.0, min(seconds, LISTEN_PROBE_MAX_S))
        try:
            info = TUNER.start(
                int(round(mhz * 1_000_000)),
                mode,
                body.get("gain"),
                purpose=PURPOSE_LISTEN,
                serial=named,
            )
        except SdrBusy as busy:
            self._json(409, {"detail": str(busy)})
            return
        except (SdrError, ValueError) as bad:
            self._json(400, {"detail": str(bad)})
            return
        try:
            answer = self._watch_listen(
                seconds,
                info.session_id,
                want_audio=want_audio,
                want_band=want_band,
                retune_hz=int(round(retune_mhz * 1_000_000)) if retune_mhz else 0,
            )
        finally:
            with contextlib.suppress(Exception):
                TUNER.stop(info.session_id)
        self._json(200, answer)

    def _watch_listen(
        self,
        seconds: float,
        session_id: str,
        *,
        want_audio: bool = False,
        want_band: bool = False,
        retune_hz: int = 0,
    ) -> dict[str, Any]:
        """Hold a listening session for `seconds` and reduce it to a verdict."""
        session = TUNER.find(session_id)
        if session is None:
            return {
                "ok": False,
                "engine": None,
                "summary": "the session was gone before the probe looked",
                "findings": ["nothing held the radio by the time the probe looked"],
            }
        if want_audio:
            session.tap_audio(seconds)
        # THE CHANNEL, named rather than defaulted: the same session now also publishes
        # the band it is sitting in, and `_listen_verdict` reads every row it is handed as
        # a picture of the tuned channel. A band row reaching that arithmetic would report
        # a station's own neighbours as its SNR.
        sub = (
            session.subscribe_frames(listen.VIEW_CHANNEL)[0] if session.draws_frames else None
        )
        # ...and the BAND, when asked, off the very same capture. This is what W3 buys
        # and the only way to see it without a browser: what else was on the air while
        # this radio was listening to one channel of it, measured from the same samples
        # the audio came from rather than from a second session that cannot exist.
        # Subscribing is also what turns the band transform ON (`capture.BandSink`), so
        # a probe that does not ask costs nothing.
        band_sub = (
            session.subscribe_frames(listen.VIEW_BAND)[0]
            if want_band and session.draws_frames
            else None
        )
        frames: list[listen.Frame] = []
        band_rows: list[listen.Frame] = []
        peaks_seen: list[float] = []
        clip_seen: list[float] = []
        rms_seen: list[float] = []
        # HALFWAY, so there is a comparable stretch of frames either side of it. The
        # retune is what A2 claims costs one dropped frame instead of a pipeline
        # rebuild, and "the session id survived" cannot tell those apart — only the gap
        # between two consecutive rows can, and only the stream token can say the stream
        # was never rebuilt at all.
        turn_at = started_wall = 0.0
        turned: dict[str, Any] = {}
        before_token = session.stream_token
        started = time.monotonic()
        deadline = started + seconds
        turn_at = started + seconds / 2.0 if retune_hz else 0.0
        try:
            while time.monotonic() < deadline:
                if turn_at and time.monotonic() >= turn_at:
                    turn_at = 0.0
                    started_wall = time.time()
                    try:
                        session.tune(retune_hz)
                        turned["ok"] = True
                    except Exception as bad:  # noqa: BLE001 - a refusal is the finding
                        turned["ok"] = False
                        turned["refused"] = str(bad)
                    turned["at"] = started_wall
                left = max(0.05, deadline - time.monotonic())
                if sub is None:
                    time.sleep(min(0.1, left))
                else:
                    try:
                        frame = sub.get(timeout=left)
                    except queue.Empty:
                        break
                    if frame is None:
                        break
                    frames.append(frame)
                # Sampled alongside the frames rather than once at the end: a peak read
                # after the fact is whatever the last buffer happened to hold, and a
                # channel that was loud for four seconds and quiet for the fifth would
                # read as silent.
                peaks_seen.append(session.audio_peak)
                clip_seen.append(session.audio_clipped)
                rms_seen.append(session.audio_rms)
                if band_sub is not None:
                    # Drained rather than waited on: the band row is a bonus reading and
                    # must never be what decides how long the probe takes.
                    with contextlib.suppress(queue.Empty):
                        while True:
                            row = band_sub.get_nowait()
                            if row is None:
                                break
                            band_rows.append(row)
        finally:
            if sub is not None:
                session.unsubscribe_frames(sub)
            if band_sub is not None:
                session.unsubscribe_frames(band_sub)
        pcm = session.taken_audio() if want_audio else b""
        verdict = _listen_verdict(
            session,
            frames,
            peaks_seen,
            clip_seen,
            rms_seen,
            round(time.monotonic() - started, 2),
        )
        if retune_hz:
            verdict["retune"] = _retune_report(
                turned, frames, before_token, session.stream_token, retune_hz
            )
        if want_band:
            verdict["band"] = _band_report(band_rows, session.tuner_gain_db)
        if pcm:
            # Base64 in the verdict rather than a second route: the audio is only ever
            # wanted ALONGSIDE the numbers it explains, and the caller that asked for it
            # (`api/debug.py`) strips it once whisper has read it.
            verdict["audio_wav_b64"] = base64.b64encode(_wav(pcm, listen.AUDIO_RATE)).decode()
            verdict["audio_seconds"] = round(len(pcm) / 2 / listen.AUDIO_RATE, 2)
        return verdict

    def _listen(self, body: dict[str, Any]) -> None:
        # Absent means listening: every existing caller predates purposes and means
        # exactly that, so the default keeps them byte-identical.
        purpose = str(body.get("purpose") or PURPOSE_LISTEN)
        sweep = None
        if purpose in SWEEPING:
            try:
                # Per PURPOSE, because the two sweeping purposes run different engines:
                # `survey` is rtl_power and cannot see shortwave at all, `spectrum` does
                # its own FFT off raw I/Q and can.
                sweep = _range_of(body)
            except (TypeError, ValueError) as bad:
                self._json(400, {"detail": f"a range needs numbers: {bad}"})
                return
            except ListenError as refused:
                self._json(400, {"detail": str(refused)})
                return
        try:
            info = TUNER.start(
                # Zero is fine for a sweeping purpose: `Session` replaces it with the
                # range's centre, which is the only frequency a span has.
                frequency_hz=int(body.get("frequency_hz", 0)),
                mode=str(body.get("mode", "fm")),
                gain=body.get("gain"),
                purpose=purpose,
                sweep=sweep,
                # Which radio, resolved by the api from the owner's settings. Absent
                # keeps the historical "whatever enumerates first" for a one-dongle box.
                serial=listen.validate_serial(body.get("serial")),
                # Absent means the mode's default, so a client that predates the
                # bandwidth control gets exactly what it always got.
                bandwidth_hz=body.get("bandwidth_hz"),
            )
        except ListenBusy as busy:
            self._json(409, {"detail": str(busy)})
            return
        except (ListenError, ValueError) as bad:
            self._json(400, {"detail": str(bad)})
            return
        self._json(200, info.as_dict())

    def _view_span(self, body: dict[str, Any]) -> None:
        """How wide the tuning picture is drawn. **Not a retune.**

        Its own route rather than a field on `/listen/tune` precisely because it is not:
        `tune` rebuilds the demodulator, which is right for a filter change and wrong for
        a zoom — it would click the audio every time the owner changed magnification. So
        this reaches `set_view_span`, which stores one number and lets the next frame be
        cropped to it."""
        wanted = body.get("session_id")
        session = TUNER.find(str(wanted)) if wanted else TUNER.for_purpose(PURPOSE_LISTEN)
        if session is None:
            self._json(
                409,
                {
                    "detail": "that session is no longer the live one"
                    if wanted
                    else "nothing is listening"
                },
            )
            return
        try:
            session.set_view_span(int(body.get("span_hz", 0)))
        except (ListenError, ValueError) as bad:
            self._json(400, {"detail": str(bad)})
            return
        self._json(200, session.info().as_dict())

    def _tune(self, body: dict[str, Any]) -> None:
        # By id when the caller names one — with several radios live, "retune the
        # session" is not a request that identifies anything. Falling back to the
        # listening session keeps a one-radio caller working unchanged.
        wanted = body.get("session_id")
        # A body carrying a RANGE is asking about the waterfall, not the tuner, and
        # "the session" is not one thing on a box with two radios. Naming the id is
        # what the api actually does; this is what makes the route readable without it.
        ranged = body.get("start_hz") is not None
        if wanted:
            session = TUNER.find(str(wanted))
        else:
            session = TUNER.for_purpose(PURPOSE_SPECTRUM if ranged else PURPOSE_LISTEN)
        if wanted and session is None:
            # `find` matches on id, so a named session that is not here is a STALE id —
            # the session was replaced or released while the sheet held it. Said plainly:
            # an earlier cut left the old "no longer the live one" check below `find`,
            # where it could never fire, and this case fell through to "nothing is
            # listening" — false whenever a listening session existed, and not something
            # the owner could act on.
            self._json(409, {"detail": "that session is no longer the live one"})
            return
        if session is not None and session.purpose == PURPOSE_SPECTRUM:
            self._resweep(session, body)
            return
        if session is not None and session.purpose != PURPOSE_LISTEN:
            # Retuning a logging session would move the packet channel to wherever the
            # tuner sheet asked for, leave the lease claiming to be logging APRS, and
            # refuse the next caller with a reason that had become false. Releasing is
            # the only honest way out of one job into another.
            self._json(
                409,
                {
                    "detail": f"the radio is {PURPOSE_LABEL.get(session.purpose, 'in use')}"
                },
            )
            return
        if session is None:
            doing = "watching the spectrum" if ranged else "listening"
            self._json(409, {"detail": f"nothing is {doing}"})
            return
        try:
            session.tune(
                int(body.get("frequency_hz", 0)),
                body.get("mode"),
                body.get("bandwidth_hz"),
            )
        except listen.SessionGone:
            # Released between resolving it above and retuning it. `tune` refuses rather
            # than relaunching, because a relaunch here would spawn an rtl_fm for a
            # session no longer in the registry: invisible to `blocking_key`, unreapable,
            # and holding the dongle until the container restarts — after which the next
            # caller for that serial succeeds and two processes fight over one radio.
            self._json(409, {"detail": "that session is no longer the live one"})
            return
        except (ListenError, ValueError) as bad:
            self._json(400, {"detail": str(bad)})
            return
        self._json(200, session.info().as_dict())

    def _resweep(self, session: listen.Session, body: dict[str, Any]) -> None:
        """Point a live spectrum somewhere else, on the session it is already holding.

        Through the SAME route as a retune because it is the same move — the radio stays
        leased, the session id stays good, the viewers stay attached — and a second route
        would be a second place for the released-session race to be got wrong."""
        try:
            # Only a live spectrum is ever resweept, and that engine reaches shortwave.
            sweep = _range_of(body)
        except (TypeError, ValueError) as bad:
            self._json(400, {"detail": f"a range needs numbers: {bad}"})
            return
        except ListenError as refused:
            self._json(400, {"detail": str(refused)})
            return
        try:
            session.resweep(sweep)
        except listen.SessionGone:
            # Released between resolving it above and moving it. `resweep` refuses
            # rather than relaunching, for the reason `Session._restart` gives.
            self._json(409, {"detail": "that session is no longer the live one"})
            return
        except (ListenError, ValueError) as bad:
            self._json(400, {"detail": str(bad)})
            return
        self._json(200, session.info().as_dict())

    def _stop(self, body: dict[str, Any]) -> None:
        """Release one session. Naming none used to be unambiguous; now it is a choice.

        With one radio there was one session, so "release the radio" could not mean
        anything else. With a radio each for APRS and the tuner, releasing whatever
        happens to be first would let jerv's "release the radio" stop a log the owner
        armed on a schedule — a silent loss, in the direction that looks like success.

        So an unnamed stop takes the LISTENING session and nothing else. An earlier cut
        fell back to "the only session when there is exactly one", reasoning that a
        one-dongle box has nothing to choose between — but the condition it actually
        tested was `len(sessions) == 1`, which is equally true of a TWO-dongle box
        running only APRS. It would have stopped the log there, which is the outcome
        this paragraph says it prevents.

        The cost is a real behaviour change on a one-dongle box: "release the radio"
        while APRS holds it now answers `stopped: false` instead of stopping the log.
        That is the honest answer — the caller is told nothing was listening, and the
        APRS switch is one tap away — and it is the direction that cannot lose a log the
        owner armed on a schedule. `holding` says what IS on a radio, so the caller can
        name it rather than leaving the owner to guess.
        """
        session_id = body.get("session_id")
        if session_id is None:
            chosen = TUNER.for_purpose(PURPOSE_LISTEN)
            if chosen is None:
                self._json(
                    200,
                    {
                        "stopped": False,
                        "holding": [
                            {"purpose": s.purpose, "serial": s.serial, "session_id": s.id}
                            for s in TUNER.sessions()
                        ],
                    },
                )
                return
            session_id = chosen.id
        self._json(200, {"stopped": TUNER.stop(str(session_id))})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
        route = self.path.split("?")[0]
        if route in ("/listen/start", "/listen/tune", "/listen/stop", "/listen/view"):
            body = self._body()
            if body is None:
                return
            if route == "/listen/start":
                self._listen(body)
            elif route == "/listen/tune":
                self._tune(body)
            elif route == "/listen/view":
                self._view_span(body)
            else:
                self._stop(body)
            return
        if route == "/sweep":
            self._sweep()
            return
        if route == "/reset":
            body = self._body()
            if body is not None:
                self._reset(body)
            return
        if route == "/soapy/probe":
            body = self._body()
            if body is not None:
                self._soapy_probe(body)
            return
        if route == "/spectrum/probe":
            body = self._body()
            if body is not None:
                self._spectrum_probe(body)
            return
        if route == "/listen/probe":
            body = self._body()
            if body is not None:
                self._listen_probe(body)
            return
        if route != "/capture":
            self._json(404, {"detail": "not found"})
            return
        body = self._body()
        if body is None:
            return

        try:
            result = capture(
                freq_hz=int(body.get("frequency_hz", 0)),
                seconds=body.get("seconds", 8),
                mode=str(body.get("mode", "fm")),
                gain=body.get("gain"),
                serial=listen.validate_serial(body.get("serial")),
            )
        except SdrBusy as busy:
            self._json(409, {"detail": str(busy)})
            return
        except (SdrError, ValueError) as bad:
            self._json(400, {"detail": str(bad)})
            return
        except OSError as missing:  # rtl_fm absent from the image
            self._json(500, {"detail": f"rtl_fm could not run: {missing}"})
            return

        wav = result.pop("wav")
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav)))
        # The metadata rides in headers so the body stays a plain WAV the caller can
        # hand straight to whisper without unwrapping an envelope.
        self.send_header("X-Sdr-Meta", json.dumps(result))
        self.end_headers()
        self.wfile.write(wav)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # `format`, shadowing the builtin, because that is what BaseHTTPRequestHandler
        # names this parameter and a caller passing it by keyword would otherwise miss.
        #
        # Default logging writes to stderr per request; the container log is the
        # audit trail we want, so keep it but without the noisy address prefix.
        print(f"[sdr] {format % args}", flush=True)  # noqa: T201


def main() -> None:
    port = int(os.environ.get("SDR_PORT", "8000"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()
