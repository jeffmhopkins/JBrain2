"""The live listening session: one tuned radio, streamed as MP3 while it runs.

This is the lease made real. One Session per RADIO, and a second caller for a radio
already held is told 409 rather than queued — an unknown wait on a radio someone else
is using is worse than a plain no (docs/plans/SDR_RADIO_PLAN.md §4.2). It was one
Session per BOX until APRS_CONTROL_PLAN.md P0b, which is the same thing while there is
one dongle and is what made APRS logging and the tuner take turns once there were two.
The session's EXISTENCE is what the omnibox icon reads, so "is a session running" and
"does the owner see a radio icon" are the same fact rather than two that can disagree.

**Why two subprocesses.** `rtl_fm` emits raw signed-16-bit mono PCM; browsers do not
play that. We could pipe rtl_fm straight into the encoder, but then nothing can see
the samples — and the level meter is the one honest signal that separates a working
capture from a dead antenna (whisper will confabulate words over noise, so the
transcript cannot be trusted for that judgement). So the PCM comes through Python:
we measure it, then write it on to ffmpeg, which encodes MP3 that an ordinary
`<audio>` element plays from a chunked response.

**Why MP3 and not Opus-in-WebM.** Listeners join LATE and REPEATEDLY: one session
outlives many openings of the tuner sheet, and a retune relaunches the encoder under
whoever is already listening. A container with an initialisation header — WebM — only
decodes for a listener who was there to receive that header, so everyone who attached
later got a headerless stream their browser could not start, and a retune spliced a
SECOND header into connections already in progress. MP3 has no header: it is a
sequence of self-describing frames, so a listener can begin at any byte and a
relaunched encoder simply continues the stream. That is the property this shape needs,
and it is why internet radio has been served this way for thirty years.

**Retuning restarts the pipeline, and must not hang up on anyone.** `rtl_fm` cannot be
retuned in place, so a tune tears down and relaunches at the new frequency while
keeping the SESSION id — the UI sees a continuous session with a brief audio gap
rather than one that vanished and came back, which would flicker the icon and read as
the lease having been dropped. Subscribers stay attached across that restart: the
end-of-stream sentinel means the SESSION is over, never that its pipeline is being
replaced. Sending it on a retune closed every listener's connection, and because the
browser had seconds of audio buffered, it played on and then went silent — a failure
that looks like a dead radio several seconds after the thing that actually caused it.
"""

from __future__ import annotations

import collections
import contextlib
import os
import dataclasses
import queue
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

import peaks

import iq
import packets
import radio

#: What the R820T2 TUNER reaches. Not the floor of the radio — see DIRECT_MIN_HZ.
MIN_HZ = 24_000_000
MAX_HZ = 1_766_000_000
#: What the RTL2832U's ADC reaches with the tuner bypassed. The NESDR SMArt v5 feeds HF
#: to the ADC's **Q branch** through an on-board diplexer, so `-E direct2` reaches it
#: with no hardware mod. `-E direct` is the I branch, which this board does not wire —
#: which is also why `rtl_power` can never sweep down here: it hardcodes mode 1.
DIRECT_MIN_HZ = 100_000
#: The RTL2832U's ADC clock, and half of it: the top of the FIRST Nyquist zone. Mirrored
#: from `jbrain/sdr/bands.py` (`ADC_RATE_HZ`, `NYQUIST_HZ`) for the reason `MIN_HZ` is —
#: this process is the one that opens the device, so a bound that lives only in the
#: caller is not a bound once there is a second caller.
ADC_RATE_HZ = 28_800_000
NYQUIST_HZ = ADC_RATE_HZ // 2
MODES = {
    "fm": "fm",
    "nfm": "fm",
    "wbfm": "wbfm",
    "am": "am",
    "usb": "usb",
    "lsb": "lsb",
}

# What a session is HOLDING a radio for. The lease is PER RADIO (APRS_CONTROL_PLAN P0b),
# so a refusal has to name both — which radio, and which job has it — because on a
# two-dongle box "the radio is busy" is not even true, let alone actionable. Which job
# also decides what the loser is told: "release it to listen" and "release it to log"
# are opposite advice (docs/plans/APRS_CONTROL_PLAN.md P0).
# `listen` produces audio; `aprs` decodes packets and produces none.
PURPOSE_LISTEN = "listen"
PURPOSE_APRS = "aprs"
# A band sweep. Unlike the other two it ENDS ON ITS OWN when rtl_power's exit timer
# fires, so the radio frees itself — but it is still a real Session while it runs, which
# is the whole point: the omnibox icon, the elapsed clock, Release and the 409 all read
# the session's existence, so a sweep that held the radio outside the lease would be a
# radio held by something invisible.
PURPOSE_SURVEY = "survey"
# A LIVE spectrum: the same rtl_power, with no exit timer and its CSV on stdout instead
# of a file, so rows can be fanned out as they are measured rather than read back once.
# The difference from `survey` is the whole point — a survey is a measurement that ends
# and is then reduced, this is a picture that keeps being drawn — so it is a purpose of
# its own rather than a flag on that one: they end differently, they are released
# differently, and the omnibox has to name them differently.
PURPOSE_SPECTRUM = "spectrum"
# Every purpose maps to the phrase a refusal uses. `.get` rather than `[]` because this
# is read while holding the tuner lock on the contention path: a purpose added without a
# label would otherwise raise KeyError there and turn a 409 into a 500 with a traceback.
PURPOSE_LABEL = {
    PURPOSE_LISTEN: "listening",
    PURPOSE_APRS: "logging APRS",
    PURPOSE_SURVEY: "sweeping the band",
    PURPOSE_SPECTRUM: "watching the spectrum",
}
PURPOSES = tuple(PURPOSE_LABEL)
#: The purposes tuned to a RANGE rather than a frequency. Both drive rtl_power, and
#: every place that asks "does this session have a sweep" means this set — which is one
#: line rather than two comparisons that can fall out of step.
SWEEPING = (PURPOSE_SURVEY, PURPOSE_SPECTRUM)

AUDIO_RATE = 16_000  # whisper's native rate, and rtl_fm's for narrowband
# Sweep bounds, enforced HERE rather than at the caller. An agent will ask for an hour,
# because nothing in its training says the radio is scarce — and a survey holds the
# tuner for every second of it.
MAX_SWEEP_SECONDS = 900
MIN_SWEEP_BIN_HZ = 100
MAX_SWEEP_BIN_HZ = 100_000
MAX_SWEEP_SPAN_HZ = 60_000_000
# How long a non-session claim on a radio (a one-shot `capture`) stays good. Longer than
# any capture the sidecar will run — server.py caps one at 120 s — plus room for device
# open and tuner settle on a cold radio, so an expiry is always a LEAK rather than a slow
# caller. See `Tuner._holders` for why a promise to release is not enough.
RESERVATION_TTL_S = 300.0

# --- live spectrum --------------------------------------------------------------
# One waterfall row per second. rtl_power retunes WITHIN the interval rather than
# stretching it (measured — `_sweep_cmd` says so at length), so this is the real frame
# rate at every span the sweep bounds allow, not one divided by the hop count.
SPECTRUM_INTERVAL_S = 1
# Frames held for one viewer. Two seconds' worth: a waterfall row that arrives late is
# drawn in the wrong place, so a viewer that has stopped reading is dropped rather than
# queued — the same backpressure, and for the same reason, as live audio.
SPECTRUM_QUEUE = 4
# Widest a live spectrum may be, in bins. Every bin crosses a websocket once a second
# per viewer, and 4096 is already more than a pixel per bin on any screen the PWA runs
# on — so this is where "any width" stops being free. A caller that wants more span
# asks for coarser bins.
SPECTRUM_MAX_BINS = 4096
#: Segments Welch-averaged into one I/Q waterfall row. Eight trades a little time
#: resolution for a floor steady enough to read a weak carrier off — the same averaging
#: the F0 probe uses, and the reason its 99.3 MHz capture showed 29 dB of headroom
#: rather than a noisy 10. It also sets the frame rate: `rate / (bins * 8)` is 73 fps at
#: 2.4 MS/s and 4000 bins, so the limit on this path is the wire, never the radio.
IQ_SEGMENTS = 8
#: Rows per second on the wire. Not a limit of the engine — it measured 31.9 fps on the
#: box — but of everything downstream: the plan's own budget is 5,704 B gzipped per
#: frame, so 31.9 fps is ~1.45 Mbit/s per viewer against the ~0.46 it was sized for,
#: plus thirty 4096-element JSON parses a second on a phone. Ten was the number every
#: F5 decision was made against (`SPECTRUM_QUEUE = 4` is 0.4 s of slack at ten and
#: 0.125 s at thirty-two).
TARGET_FPS = 10.0
#: The most a single frame may average. Bounds the read buffer, and past this the row
#: is a long enough exposure that a burst inside it is smeared rather than seen.
MAX_IQ_SEGMENTS = 128
#: Segments per HOP. Small on purpose: a hopping sweep is already time-limited by the
#: retunes, so averaging harder inside a hop buys noise performance at the price of the
#: one thing that is scarce here.
HOP_SEGMENTS = 4


def segments_for(bins: int, rate_hz: int) -> int:
    """How many segments to average so a row takes about `1 / TARGET_FPS`.

    Slowing to the wire budget by AVERAGING MORE rather than by throwing frames away:
    the samples arrive either way, so discarding them buys nothing and costs the noise
    floor. A row that averages 100 ms is exactly what a 10 fps row should be, and the
    floor it reports is steadier than one built from a tenth of the data."""
    if bins <= 0 or rate_hz <= 0:
        return IQ_SEGMENTS
    want = round(rate_hz / (bins * TARGET_FPS))
    return max(IQ_SEGMENTS, min(int(want), MAX_IQ_SEGMENTS))


def hop_usable_bins(bins: int) -> int:
    """The trusted middle of one hop, in bins, and always EVEN.

    The same 5/6 the api plans against (`bands.TRUSTED_FILL`), duplicated for the reason
    every other constant here is: `deploy/sdr/` ships in its own container and imports
    nothing from the backend. Even, because the hop's centre sits on the boundary
    between its two middle bins and a half-bin error accumulates across a dozen hops."""
    return (bins * 5 // 6) // 2 * 2


def hop_centres(start_hz: int, rate_hz: int, bins: int, hops: int) -> list[int]:
    """Where each hop tunes, so the trusted middles tile with no gap and no overlap."""
    usable = hop_usable_bins(bins)
    width = iq.bin_width_hz(rate_hz, bins)
    return [
        int(round(start_hz + (index * usable + usable / 2) * width))
        for index in range(hops)
    ]

#: Whether `stdbuf` is here to make rtl_power's stdout line-buffered. Resolved once, at
#: import, rather than per launch: it is a property of the image, and a `which` on every
#: retune is a syscall for an answer that cannot have changed.
_LINE_BUFFERED = shutil.which("stdbuf") is not None

#: How long a freshly launched pipeline is watched before it is believed. Paid on every
#: start, which is the cost of a 200 meaning "the radio opened" instead of "a process
#: was spawned".
#:
#: An earlier version of this comment said the tools "fail in milliseconds", and the box
#: disproved it: `rtl_power` printed `No matching devices found` and then carried on
#: printing its hop plan, still running when the grace expired. So waiting for the
#: process to DIE is not a reliable way to notice — which is why `_CANNOT_OPEN` exists
#: and this window is the backstop rather than the mechanism.
STARTUP_GRACE_S = 0.4

#: What the rtl_* tools say when they cannot have the radio. MATCHED ON rather than
#: waited for: the tool announces the failure long before it acts on it, so its own
#: words are both faster and more certain than watching for an exit. Lower-cased at the
#: comparison, because librtlsdr and the tools around it are not consistent about case.
_CANNOT_OPEN = (
    "no matching devices found",
    "no supported devices found",
    "usb_open error",
    "failed to open rtlsdr device",
)
#: How many lines of a tool's own stderr to keep for a refusal. Enough to carry
#: librtlsdr's device list, which is what says WHICH radio it could not find.
_LOG_TAIL = 12

#: Child processes a teardown could not confirm dead.
#:
#: `_kill` clears `_rtl`/`_enc` unconditionally, because `alive` has to be able to go
#: false. That is right for the session and wrong for the PROCESS: a survivor holds the
#: dongle against every later caller with nothing left in the program pointing at it,
#: and this container owns the radio exclusively, so there is no such thing as a
#: legitimate orphan. Handles that outlive their kill are parked here and retried on
#: every reap instead of being dropped on the floor.
_survivors: list[subprocess.Popen[bytes]] = []
_survivors_lock = threading.Lock()


def _park(proc: subprocess.Popen[bytes]) -> None:
    with _survivors_lock:
        _survivors.append(proc)


def reap_survivors() -> int:
    """Kill whatever an earlier teardown could not, and forget what has gone. Count left.

    Called from `Tuner._reap`, so it runs on the same beat as the session sweep and
    needs no timer of its own."""
    with _survivors_lock:
        pending, _survivors[:] = list(_survivors), []
    left: list[subprocess.Popen[bytes]] = []
    for proc in pending:
        if proc.poll() is not None:
            continue
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            proc.kill()
            proc.wait(timeout=1)
        if proc.poll() is None:
            left.append(proc)
    if left:
        with _survivors_lock:
            _survivors.extend(left)
    return len(left)
# How long a parsed audio level stays claimable by an arriving frame. The measured
# pairing is sub-millisecond; this is slack for a loaded box, not a guess.
_LEVEL_WINDOW_S = 2.0
AUDIO_BITRATE = (
    "64k"  # MP3 at 16 kHz mono; the demodulated audio is the ceiling, not this
)
# Wide FM's demodulation rate. **192 k, not rtl_fm's documented 171 k**, for two
# measured reasons that both bite at 171:
#
# 1. A broadcast FM station deviates ±75 kHz and carries audio to 53 kHz (stereo
#    subcarrier and RDS above that), so Carson's rule puts its occupied bandwidth near
#    190 kHz. At `-s 171000` the demodulator sees ±85.5 kHz and CLIPS THE STATION'S OWN
#    SIDEBANDS — the distortion is in the signal path before anything can fix it.
# 2. rtl_fm resamples to `-r` with `low_pass_real`, whose decimation factor is the
#    INTEGER `rate_in / rate_out`. 171000/16000 = 10.6875 truncates to 10, so the
#    output is 6.9% fast and each sample averages a number of inputs that alternates
#    between 10 and 11 — a per-sample gain wobble on every wide-FM capture we have ever
#    taken. 192000/16000 = 12 exactly, and 192 kHz clears Carson.
#
# Measured on the box before the change: 92.3 MHz read `Oversampling input by: 6x`,
# `1026000 S/s`, `Output at 171000 Hz` — confirming both.
WBFM_SAMPLE_RATE = 192_000
AUDIO_CONTENT_TYPE = "audio/mpeg"
_CHUNK = 4096

# --- live captioning ------------------------------------------------------------
# Segments are cut for whisper, which is not a streaming model: it transcribes a
# finished clip, so captions are chunks of live audio sent one after another.
#
# Chunk length is a LATENCY choice, not a throughput one. Measured on the box across 26
# consecutive calls with the model resident: ~9.8 s per transcription whatever the clip
# holds. Flat because whisper.cpp pads every clip to a 30 s window — NOT because the
# time is model loading, which is what an earlier reading of the same number concluded.
# So a second of extra audio really is close to free, and the api merges whatever has
# piled up into one clip rather than transcribing segments one at a time. Short segments
# buy responsiveness at no real cost; the floor is what makes a sentence long enough to
# transcribe well.
SEGMENT_MIN_S = 3.0
SEGMENT_MAX_S = 8.0
# A segment ends on a quiet gap rather than a clock, so cuts fall between words.
SEGMENT_GAP_LEVEL = 0.06
SEGMENT_GAP_CHUNKS = 3
# The squelch: below this, a segment is noise and is never sent (see Session._cut).
SEGMENT_SQUELCH = 0.12
SEGMENT_QUEUE = 8

# Decoded frames waiting for a reader. Small: a packet channel is quiet, and a reader
# that has stopped reading is gone rather than briefly behind.
PACKET_QUEUE = 32
# Direwolf's KISS port, chosen per session from its id so a relaunch cannot land on a
# port the previous process has not finished releasing.
KISS_PORT_BASE = 8200
KISS_PORT_SPAN = 100

# A subscriber that stops reading (a closed tab, a stalled phone) must not wedge the
# pump or grow without bound. Its queue is small and we DROP for that subscriber
# rather than block everyone — live audio is worthless late, so dropping is the
# correct backpressure here.
_SUB_QUEUE_CHUNKS = 64


class SdrBusy(RuntimeError):
    """This radio is already held — one session per RADIO, not per box, so the other
    dongle is unaffected and a refusal names the one it is about. A holder that named no
    radio holds them all, because nothing can prove which one it opened."""


class SdrError(RuntimeError):
    """The radio could not be tuned, or the pipeline could not start."""


class RadioUnavailable(SdrError):
    """The I/Q engine could not have the radio.

    A KIND of `SdrError`, not a sibling: where it is not caught it must refuse like any
    other sidecar failure — a subclass of `RuntimeError` would have escaped every
    `except SdrError` on the way out and become a 500 with a traceback instead of a
    sentence. It has its own name so the spectrum path can catch exactly this one and
    fall back rather than refuse (CLAUDE.md #10: no terminal, so no revert-and-rebuild
    to get a picture back), which a bare `except SdrError` would over-reach into."""


class SessionGone(RuntimeError):
    """This session was released, so it may not relaunch anything."""


def aliased_refusal(frequency_hz: int) -> str | None:
    """Why direct sampling cannot honestly tune here, or None.

    THE GAP BETWEEN THE TWO FLOORS. `demod_args` puts everything below `MIN_HZ` on
    `-E direct2`, where the ADC samples at 28.8 MHz and the first Nyquist zone ends at
    14.4 MHz. Between 14.4 and 24 MHz the tuner is out of circuit and the request lands
    in the SECOND zone, which folds: ask for 18.1 MHz and the radio hands back 10.7 MHz,
    mirrored, with sideband sense inverted. Right signal path, wrong station, and
    nothing in the audio says so — which is why this is a refusal rather than a note.

    Only the sidecar can state the whole rule, because only the sidecar knows both that
    `direct2` was chosen and what clock it implies (docs/plans/SDR_IQ_SPECTRUM_PLAN.md
    §8, "a pre-existing bug this makes visible"). Above `MIN_HZ` the R820T2 is back in
    circuit and mixes properly, so there is nothing here to refuse."""
    if NYQUIST_HZ < frequency_hz < MIN_HZ:
        image = ADC_RATE_HZ - frequency_hz
        return (
            f"{frequency_hz / 1e6:.3f} MHz cannot be tuned: below "
            f"{MIN_HZ / 1e6:.0f} MHz this radio bypasses its tuner and samples at "
            f"{ADC_RATE_HZ / 1e6:.1f} MHz, so anything above {NYQUIST_HZ / 1e6:.1f} MHz "
            f"folds — you would hear {image / 1e6:.3f} MHz instead."
        )
    return None


def validate(frequency_hz: int, mode: str) -> str:
    """Bound the tuning request and return rtl_fm's demodulator name.

    Validated here as well as in the api because this process is the one that
    actually opens the device: a bound that lives only in the caller is a bound that
    a second caller does not have."""
    if not DIRECT_MIN_HZ <= frequency_hz <= MAX_HZ:
        raise SdrError(
            f"{frequency_hz} Hz is outside the tuner's range ({MIN_HZ}-{MAX_HZ} Hz)"
        )
    aliased = aliased_refusal(frequency_hz)
    if aliased is not None:
        raise SdrError(aliased)
    key = mode.lower()
    if key not in MODES:
        raise SdrError(f"unknown mode {mode!r} (want one of {sorted(MODES)})")
    return key


# Narrowband FM is the only thing 1200-baud AFSK arrives on. Accepting `usb` or `wbfm`
# for a logging session would start a radio that reports healthy and can never decode.
APRS_MODES = ("fm", "nfm")


#: What a USB serial may contain. librtlsdr's own strings are alphanumeric, and this is
#: the value that becomes an `rtl_fm` argv token — the only field in a /listen/start body
#: that reaches a subprocess at all.
SERIAL_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_serial(serial: object) -> str | None:
    """Bound the one field that becomes a subprocess argument.

    Here as well as in the api, for the reason `validate_purpose` gives: a bound that
    lives only in the caller is not a bound once there is a second caller — and this
    process has its own HTTP surface. Every neighbouring field is cast and checked
    (`mode` through `validate`, `purpose` through `validate_purpose`, `frequency_hz`
    through an int and a range); `serial` was taken raw off the JSON, so a dict body
    value became the argv token `serial={'a': 1}` and a dict in `/healthz`.

    None means "no radio named", which is the historical one-dongle behaviour and stays
    legal. Anything present but unusable is refused rather than silently dropped: a
    caller that asked for a specific radio and got an arbitrary one is the exact failure
    this whole path exists to prevent."""
    if serial is None or serial == "":
        return None
    if not isinstance(serial, str) or not SERIAL_RE.match(serial):
        raise SdrError(f"{serial!r} is not a usable device serial")
    return serial


#: The device key a session with no serial takes. Not a serial, and cannot collide with
#: one: `validate_serial` requires at least one character.
ANY_DEVICE = ""


def blocking_key(held: object, key: str) -> str | None:
    """Which held device stops `key` from being taken, if any.

    **An UNNAMED holder conflicts with everything, and so does an unnamed request.** A
    session or capture started with no serial opens whichever device librtlsdr
    enumerates first, so nothing can prove it is not on the radio a second caller is
    asking for. Refusing is the only honest answer: the alternative is two processes
    fighting over one dongle, which fails as garbled audio rather than as an error. In
    practice the api resolves a serial whenever it can see the USB scan, so this is the
    one-dongle box — where one holder was always the limit — and the scan-unreachable
    case, where caution is the point.

    One function because there are two things that hold a radio (a pipeline session and
    a one-shot capture) and they must agree; the rule stated twice is a rule that can
    disagree with itself, and the symptom would be the two of them on one dongle.

    Sorted, so "which one is blocking" does not depend on dict insertion order — the
    same reason the radio list is sorted by serial.
    """
    keys = sorted(held)  # type: ignore[call-overload]
    if key in keys:
        return key
    if ANY_DEVICE in keys:
        return ANY_DEVICE
    if key == ANY_DEVICE and keys:
        return keys[0]
    return None


def demod_args(mode: str, gain: str | None, frequency_hz: int) -> list[str]:
    """Everything after `-f` and `-d` that decides how a signal is DEMODULATED.

    One function because there are two callers — a live session and the one-shot
    `capture` — and they were building this list separately. That is how `-d serial=`
    shipped wrong in both places at once: a setting that lives in two builders is a
    setting that will eventually differ between them, and the symptom is a capture that
    sounds unlike the live audio of the same station.

    `-F 9` on every mode. rtl_fm's default decimator is a BOXCAR — an unweighted sum,
    whose first sidelobe is only ~13 dB down — so a strong neighbour a few channels away
    leaks into whatever you are tuned to. `-F 9` swaps it for cascaded half-band stages
    with droop correction: real adjacent-channel rejection at the SAME bandwidth, which
    makes it the one filter improvement that costs nothing. It forces the decimation to
    a power of two (63 → 64 narrowband), so the device lands on 1.024 MS/s instead of
    1.008.

    `-E dc` on AM only. An AM carrier is a DC pedestal after envelope detection; left in,
    it eats headroom and reaches whisper as an offset on every sample. FM does not have
    one — a discriminator's output is already centred — so this would only add a filter
    with nothing to remove."""
    args = ["-F", "9"]
    if frequency_hz < MIN_HZ:
        # Below the tuner, the ADC is fed straight from the antenna. `direct2` selects
        # the Q branch, which is the one this board wires; `direct` would select I and
        # produce silence on hardware that looks otherwise healthy.
        args += ["-E", "direct2"]
    if MODES[mode] == "wbfm":
        args += ["-s", str(WBFM_SAMPLE_RATE), "-r", str(AUDIO_RATE)]
    else:
        args += ["-s", str(AUDIO_RATE)]
    if MODES[mode] == "am":
        args += ["-E", "dc"]
    if gain and frequency_hz >= MIN_HZ:
        # Deliberately dropped below the tuner's floor: `rtlsdr_set_direct_sampling`
        # calls the tuner's own `exit()`, so the R820T2 is powered down and out of the
        # signal path. `-g` there writes to a chip that is not listening, and the
        # honest thing is to not claim a control that does nothing.
        args += ["-g", gain]
    return args


def validate_purpose(purpose: str) -> str:
    """Bound the job a session may hold the tuner for.

    Here as well as in the caller for the same reason `validate` bounds frequency and
    mode here: a bound that lives only in the caller is not a bound once there is a
    second caller."""
    if purpose not in PURPOSES:
        raise SdrError(f"unknown purpose {purpose!r} (want one of {sorted(PURPOSES)})")
    return purpose


def _peak(pcm: bytes) -> float:
    """Loudest sample in a chunk, as a 0..1 fraction of full scale."""
    count = len(pcm) // 2
    if not count:
        return 0.0
    return max(abs(v) for v in struct.unpack(f"<{count}h", pcm[: count * 2])) / 32768.0


@dataclass(frozen=True, slots=True)
class Sweep:
    """What a survey session is tuned to — a RANGE, where the others have a frequency.

    Clamped on construction rather than trusted, so every path that builds one gets the
    same bounds and no caller can widen them."""

    start_hz: int
    stop_hz: int
    bin_hz: int | float
    seconds: float
    #: The ONE-HOP capture the api chose for this range — `(rate_hz, bins)` — or None
    #: when no single capture covers it and `rtl_power` must hop instead. Decided there
    #: rather than here because `bands.LIVE_CAPTURES` is the band table, and a second
    #: copy of it in the sidecar is two tables that will disagree. When it is present,
    #: `bin_hz` is exactly `rate / bins` and the I/Q engine is what draws the picture.
    capture: tuple[int, int] | None = None
    #: How many captures the span takes. 1 is one tuning; more is a stitched sweep
    #: (F11), where each hop contributes the trusted middle of its own capture. The
    #: engine retunes on the LIVE stream between them, which is what F0 measured and
    #: what makes this faster than the tool it replaces rather than merely different.
    hops: int = 1
    #: One channel's width on THIS band — 200 kHz on the FM dial, 15 kHz on 2m. A band
    #: plan fact, so it comes from the api (`bands.py`) rather than being guessed from
    #: the capture: it decides which adjacent bins are one signal, and a rule that
    #: guessed would report a 200 kHz transmission as twenty stations a few kHz apart.
    #: Zero means the caller did not say, and only touching bins are then one signal.
    channel_hz: int = 0

    @staticmethod
    def of(
        start_hz: int,
        stop_hz: int,
        bin_hz: int | float,
        seconds: float,
        *,
        direct_ok: bool = False,
        capture: tuple[int, int] | None = None,
        hops: int = 1,
        channel_hz: int = 0,
    ) -> "Sweep":
        """`direct_ok` says WHICH ENGINE is asking, and it is the only thing that
        changes the floor.

        `rtl_power` cannot go below `MIN_HZ` — it hardcodes direct sampling mode 1, the
        ADC's I branch, where this board wires Q — so the survey path leaves this False
        and keeps its refusal. The live spectrum reads raw I/Q and does its own FFT,
        which puts the ADC in mode 2 at runtime, so shortwave really is reachable there
        and the same floor would be a fiction copied from a tool it no longer uses
        (docs/plans/SDR_IQ_SPECTRUM_PLAN.md §6.2, F8). Two engines, two answers, one
        constructor — rather than a second Sweep type that agrees about everything else.
        """
        start, stop = int(min(start_hz, stop_hz)), int(max(start_hz, stop_hz))
        if stop - start > MAX_SWEEP_SPAN_HZ:
            raise SdrError(
                f"a {(stop - start) / 1e6:.1f} MHz span is wider than this sweep allows "
                f"({MAX_SWEEP_SPAN_HZ / 1e6:.0f} MHz)"
            )
        if stop - start < 1:
            raise SdrError("a sweep needs a range, not a single frequency")
        if direct_ok and start < DIRECT_MIN_HZ:
            raise SdrError(
                f"{start} Hz is below what this radio reaches ({DIRECT_MIN_HZ} Hz)"
            )
        if not direct_ok and start < MIN_HZ:
            # NOT a policy. `rtl_power -D` hardcodes `verbose_direct_sampling(dev, 1)` —
            # the ADC's I branch — and this board wires Q, so the tool would tune
            # something and measure nothing. Listening below the tuner works
            # (`-E direct2` on rtl_fm); sweeping there cannot, and saying so beats
            # returning a flat, plausible, meaningless waterfall.
            raise SdrError(
                f"a sweep cannot go below {MIN_HZ / 1e6:.0f} MHz — the radio reaches "
                f"shortwave by bypassing its tuner, and the sweep tool cannot use that "
                f"path. You can still listen there."
            )
        return Sweep(
            start_hz=start,
            stop_hz=stop,
            # CLAMPED, silently, which is safe only because nothing upstream now asks
            # for a width this could move: `bands.LIVE_CAPTURES` pairs each rate with an
            # N whose quotient clears MIN_SWEEP_BIN_HZ, and `bands.validate()` fails CI
            # if one ever does not. Were that to slip, a frame would declare a bin width
            # the transform never used — worse than a refusal, because nothing downstream
            # can tell (§6.14).
            # NOT clamped when the api named a capture: `rate / bins` is then the width
            # the transform actually uses, and clamping it would make the frame declare
            # a width nothing computed (§6.14). The clamp exists for rtl_power's tier,
            # where the width is a request rather than a fact.
            bin_hz=(
                bin_hz
                if capture is not None
                else max(MIN_SWEEP_BIN_HZ, min(int(bin_hz), MAX_SWEEP_BIN_HZ))
            ),
            seconds=max(1.0, min(float(seconds), MAX_SWEEP_SECONDS)),
            capture=capture,
            hops=max(1, int(hops)),
            channel_hz=max(int(channel_hz), 0),
        )

    @property
    def span_hz(self) -> int:
        return self.stop_hz - self.start_hz

    @property
    def centre_hz(self) -> int:
        """What the omnibox shows. A range has no one frequency, and showing the low
        edge would read as a tuner parked somewhere it is not."""
        return (self.start_hz + self.stop_hz) // 2

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "start_hz": self.start_hz,
            "stop_hz": self.stop_hz,
            "bin_hz": self.bin_hz,
            "seconds": self.seconds,
        }
        if self.capture is not None:
            out["rate_hz"], out["bins"] = self.capture
            out["hops"] = self.hops
        return out


def spectrum_engine_refusal(sweep: "Sweep | None") -> str | None:
    """Why the engine behind a live spectrum cannot draw that range, or None.

    The refusal belongs to the ENGINE rather than to the route, and F6 is why that
    distinction earned its keep: the same shortwave range that had to be refused while
    `rtl_power` was the only engine is drawn by the I/Q one, and a route-level floor
    would have had to be found and removed again. What is left is narrower and still
    real — `rtl_power -D` hardcodes direct sampling mode 1, the ADC's I branch, and this
    board wires Q, so a shortwave range too WIDE for one I/Q capture still has no engine
    that can honestly draw it (docs/plans/SDR_IQ_SPECTRUM_PLAN.md F6, F8).

    Whether anything is actually ARRIVING on shortwave is a separate question and
    deliberately not asked here: F0 measured a dead HF input on this box, and that shows
    up as an empty picture the owner can see rather than as a sentence about software.

    A SENTENCE RETURNED, not an exception raised, because the two callers refuse at
    different moments. Starting has nothing to lose, so `_start_spectrum_pipeline` can
    ask on its way up; a RETUNE has a running picture and a held radio to protect, so
    `resweep` has to ask before `_restart` tears either of them down. One reading, two
    call sites — the alternative is the guard drifting into two floors that disagree.
    """
    if sweep is None or sweep.start_hz >= MIN_HZ:
        return None
    if sweep.capture is not None:
        # F6: the I/Q engine sets `direct_samp` at runtime and reaches the branch this
        # board wires, so shortwave is the engine's to draw. Whether anything ARRIVES
        # there is the antenna's business and shows up as an empty picture rather than
        # as a refusal — F0 measured a dead HF input on this box, and a refusal would
        # hide that behind a sentence about software instead of showing it.
        return None
    return (
        f"a live spectrum below {MIN_HZ // 1_000_000} MHz needs one capture the I/Q "
        f"engine can draw, and this range needs several hops — which only the sweep "
        f"tool does, and it cannot reach the ADC branch this radio wires. Listening "
        f"there works."
    )


@dataclass(frozen=True, slots=True)
class Frame:
    """One waterfall row: every bin of the sweep, measured in one interval.

    SELF-DESCRIBING, and that is the point of the shape. A viewer can be told to look
    somewhere else mid-stream, and if the range lived only in the request that opened
    the stream then every frame after a retune would be drawn at the old frequencies
    until the client noticed. Carrying `start_hz`/`bin_hz` on each row means a retune
    needs no protocol event at all: the next row simply describes a different band, and
    a client that draws what each row says is already correct.

    `stop_hz` is DERIVED from the array rather than copied from rtl_power's own high
    edge, so `start_hz + i * bin_hz` addresses bin `i` exactly, whatever the tool
    reported about a block it cropped."""

    at: float
    start_hz: int
    # `int | float` since F6: the I/Q engine's width is `rate / bins`, which is exact
    # for every pairing in `bands.LIVE_CAPTURES` and a float for anything else. Rounding
    # it would make the frame declare a width the transform never used, and the PWA
    # compares this value exactly (§6.13).
    bin_hz: int | float
    db: list[float]
    #: What stands above the noise in THIS row, strongest first (`peaks.find`). Carried
    #: on the frame rather than computed by each viewer because it is a MEASUREMENT: the
    #: agent's tools and the picture must not be able to disagree about what was on the
    #: air, and only one of them is looking. Empty is a real answer — a quiet band.
    peaks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def stop_hz(self) -> int | float:
        return self.start_hz + len(self.db) * self.bin_hz

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": round(self.at, 3),
            "start_hz": self.start_hz,
            "stop_hz": self.stop_hz,
            "bin_hz": self.bin_hz,
            "bins": len(self.db),
            # One decimal. rtl_power prints two, and the second is well under the
            # noise on any real reading — it is a tenth of a dB — while it costs a
            # character per bin on every frame of every viewer's stream.
            "db": [round(v, 1) for v in self.db],
            "peaks": self.peaks,
        }


def _spectrum_row(line: str) -> tuple[str, int, int, list[float]] | None:
    """One rtl_power CSV line as (timestamp, low Hz, bin Hz, dB per bin).

    Tolerant, for the reason `sweep.reduce_csv` is: this parses text a radio wrote
    while it is still writing, so a torn or short line is a lost row rather than an
    error that ends a live picture."""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 7:
        return None
    try:
        low, step = int(float(parts[2])), int(float(parts[4]))
        values = [float(p) for p in parts[6:] if p]
    except ValueError:
        return None
    if not values or step <= 0:
        return None
    return f"{parts[0]} {parts[1]}", low, step, values


class Stitch:
    """rtl_power's rows back into whole waterfall frames.

    rtl_power emits one row per retune BLOCK, and a sweep wider than the radio's ~2.8
    MHz window is several blocks — all carrying the SAME timestamp, because the retunes
    happen inside the interval rather than extending it. So a frame is the run of rows
    sharing a timestamp.

    Waiting for the NEXT timestamp to prove a frame complete would cost every frame a
    whole interval of latency, including the single-block case that is most of what the
    PWA asks for. So the width is LEARNED — the first complete frame says how many
    blocks a frame has — and after that a frame is emitted the moment its last block
    arrives. The timestamp change is still there as the fallback, which is what makes a
    dropped row cost one short frame rather than a stalled picture.

    Blocks are keyed by their low edge rather than assumed to arrive in band order,
    because they do not: `sweep.reduce_csv` learned the same thing off the same tool.

    Pure: text in, frames out, no clock and no radio, which is what lets the awkward
    cases be tested without one."""

    def __init__(self) -> None:
        self._stamp = ""
        self._blocks: dict[int, tuple[int, list[float]]] = {}
        self._expect = 0

    def push(self, line: str) -> list[Frame]:
        """Feed one CSV line; get back whatever frames it completed (usually none)."""
        row = _spectrum_row(line)
        if row is None:
            return []
        stamp, low, step, values = row
        out: list[Frame] = []
        # A repeated block is a wrap that the timestamp did not show — belt and braces
        # against a tool that stamps two intervals alike on a slow box.
        if stamp != self._stamp or low in self._blocks:
            frame = self._flush()
            if frame is not None:
                out.append(frame)
            self._stamp = stamp
        self._blocks[low] = (step, values)
        if self._expect and len(self._blocks) >= self._expect:
            frame = self._flush()
            if frame is not None:
                out.append(frame)
        return out

    def _flush(self) -> Frame | None:
        blocks, self._blocks = self._blocks, {}
        if not blocks:
            return None
        # `max`, never plain assignment: a frame short a dropped row must not teach the
        # eager path a narrower width, or every frame after it would be cut to match.
        self._expect = max(self._expect, len(blocks))
        ordered = sorted(blocks.items())
        db: list[float] = []
        for _low, (_step, values) in ordered:
            db.extend(values)
        return Frame(
            at=time.time(),
            start_hz=ordered[0][0],
            bin_hz=ordered[0][1][0],
            db=db,
        )


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """What a session looks like from outside — the shape the omnibox tuner reads."""

    session_id: str
    frequency_hz: int
    mode: str
    gain: str | None
    started_at: float
    #: The loudest sample of the DEMODULATED AUDIO, 0..1 of full scale. **Not a signal
    #: level and not comparable to anything the spectrum path reports** (F9). This is
    #: what came out of `rtl_fm`'s discriminator after AGC, squelch and de-emphasis, so
    #: it answers "is audio coming out of the radio" and nothing about RF power: idle
    #: airband AM reads 0.21 because the tuner's AGC amplifies its own noise, while HF
    #: has no gain stage to do that. The spectrum path measures true dBFS per bin
    #: (`iq.Spectrum.db`), which is a different quantity in different units, and the
    #: name says which one this is so nothing re-surfaces it as a meter again.
    audio_peak: float
    listeners: int
    purpose: str = PURPOSE_LISTEN
    serial: str | None = None
    #: The RANGE, for the two purposes that have one. A survey and a live spectrum are
    #: tuned to a span, and `frequency_hz` can only carry its midpoint — which reads as
    #: a tuner parked somewhere it is not, and gives a waterfall no way to label its own
    #: axis. None for the purposes that really are one frequency.
    sweep: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "frequency_hz": self.frequency_hz,
            "mode": self.mode,
            "gain": self.gain,
            "purpose": self.purpose,
            "serial": self.serial,
            "started_at": self.started_at,
            "elapsed_s": round(time.time() - self.started_at, 1),
            "audio_peak": round(self.audio_peak, 4),
            "listeners": self.listeners,
            "sweep": self.sweep,
        }


class Session:
    """One tuned radio, running until stopped."""

    def __init__(
        self,
        frequency_hz: int,
        mode: str,
        gain: str | None,
        purpose: str = PURPOSE_LISTEN,
        sweep: Sweep | None = None,
        serial: str | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.sweep = sweep
        # WHICH radio this session opened. None means "whichever librtlsdr enumerates
        # first", which is the historical behaviour and is fine with one dongle plugged
        # in; with two it is how APRS silently ends up on the wrong antenna, so the api
        # resolves a serial from the owner's settings and names it here.
        self.serial = serial
        # The CSV rtl_power writes, read back once the sweep ends. Per session, so a
        # retune or a restart cannot serve a stale file.
        self.sweep_csv = f"/tmp/sweep-{self.id}.csv" if sweep else ""  # noqa: S108
        self.started_at = time.time()
        self.gain = gain
        self.purpose = validate_purpose(purpose)
        if self.purpose in SWEEPING and sweep is None:
            raise SdrError(f"a {self.purpose} session needs a sweep range")
        if self.purpose == PURPOSE_SPECTRUM and sweep is not None:
            bins = sweep.span_hz // sweep.bin_hz
            if bins > SPECTRUM_MAX_BINS:
                raise SdrError(
                    f"{bins} bins is wider than a live spectrum carries "
                    f"({SPECTRUM_MAX_BINS}); ask for coarser bins than "
                    f"{sweep.bin_hz / 1000:.0f} kHz"
                )
        if self.purpose == PURPOSE_APRS and mode.lower() not in APRS_MODES:
            raise SdrError(
                f"APRS is 1200-baud AFSK on narrowband FM; {mode!r} cannot decode it "
                f"(want one of {sorted(APRS_MODES)})"
            )
        if self.sweep is not None:
            # A range has no ONE frequency, and its centre is what the omnibox draws and
            # what a refusal names. Set here rather than trusted from the caller: three
            # of them build a sweeping session and only one was doing this, so a
            # waterfall started from the api reported the low edge as its tuning.
            frequency_hz = self.sweep.centre_hz
        self.frequency_hz = frequency_hz
        self.mode = validate(frequency_hz, mode)
        self.audio_peak = 0.0
        self._subs: set[queue.Queue[bytes | None]] = set()
        # Captioning subscribers. Segmenting only runs while at least one is attached,
        # so a session nobody is captioning does no extra work at all.
        self._segments: set[queue.Queue[tuple[float, bytes]]] = set()
        # Decoded APRS frames, for a purpose=aprs session. Empty on a listening one.
        self._packets: set[queue.Queue[packets.Packet | None]] = set()
        # Waterfall rows, for a purpose=spectrum session.
        self._frames: set[queue.Queue[Frame | None]] = set()
        # The most recent row, handed to a viewer the moment it attaches. Without it the
        # waterfall opens on a blank canvas for up to a whole interval, which reads as a
        # radio that did not start.
        self._last: Frame | None = None
        # The I/Q engine's open radio and its transform, or None on the rtl_power path.
        # `_kill` walks processes and this is not one, so teardown reaches it by name —
        # and a Radio left open is exactly the leak `/reset` must not fire under.
        self._radio: "radio.Radio | None" = None
        self._spectrometer: "iq.Spectrometer | None" = None
        # The most recent audio level direwolf announced, and when. One slot
        # rather than a queue — see `_take_audio_level`.
        self._level: tuple[float, int] | None = None
        # Per session so two sidecars, or a relaunch, cannot collide on one port.
        self.kiss_port = KISS_PORT_BASE + (int(self.id[:4], 16) % KISS_PORT_SPAN)
        self._seg: list[bytes] = []
        self._seg_started = time.time()
        self._seg_peak = 0.0
        self._seg_peak_seen = 0.0
        self._quiet_for = 0
        self._lock = threading.Lock()
        self._stopping = False
        # A retune replaces the pipeline under listeners who are still attached. It
        # sets this so the audio pump tears down WITHOUT telling them the stream is
        # over — the sentinel means the session ended, not that ffmpeg was relaunched.
        self._restarting = False
        # One way, set by `stop`. A released session must never relaunch a pipeline: see
        # `tune`, where the alternative is an rtl_fm holding a dongle nothing can see.
        self._released = False
        # The tool's own last words, for a refusal. Without them "the radio did not
        # start" is untraceable from the PWA, and the reason is sitting in a container
        # log the owner has no way to read (CLAUDE.md #10).
        self._tail: collections.deque[str] = collections.deque(maxlen=_LOG_TAIL)
        self._rtl: subprocess.Popen[bytes] | None = None
        self._enc: subprocess.Popen[bytes] | None = None
        self._threads: list[threading.Thread] = []
        self._start_pipeline()
        self._confirm_started()

    # ---- pipeline -------------------------------------------------------------

    def _rtl_cmd(self) -> list[str]:
        cmd = ["rtl_fm", "-f", str(self.frequency_hz), "-M", MODES[self.mode]]
        cmd += self._device_args()
        cmd += demod_args(self.mode, self.gain, self.frequency_hz)
        return [*cmd, "-"]

    def _enc_cmd(self) -> list[str]:
        # MP3, because a listener must be able to join mid-stream (see the module
        # docstring). -flush_packets keeps latency down; without it the muxer buffers
        # and the first sound arrives seconds late, which reads as a broken radio.
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "s16le", "-ar", str(AUDIO_RATE), "-ac", "1", "-i", "-",
            "-c:a", "libmp3lame", "-b:a", str(AUDIO_BITRATE),
            "-f", "mp3", "-flush_packets", "1", "-",
        ]  # fmt: skip

    def _start_pipeline(self) -> None:
        if self.purpose == PURPOSE_APRS:
            self._start_packet_pipeline()
            return
        if self.purpose == PURPOSE_SURVEY:
            self._start_sweep_pipeline()
            return
        if self.purpose == PURPOSE_SPECTRUM:
            self._start_spectrum_pipeline()
            return
        if shutil.which("rtl_fm") is None:
            raise SdrError("rtl_fm is not installed in this image")
        if shutil.which("ffmpeg") is None:
            raise SdrError("ffmpeg is not installed in this image")
        try:
            self._rtl = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                self._rtl_cmd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            self._enc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                self._enc_cmd(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self._kill()
            raise SdrError(f"could not start the radio pipeline: {exc}") from exc

        self._threads = [
            threading.Thread(target=self._pump_pcm, daemon=True),
            threading.Thread(target=self._pump_audio, daemon=True),
            threading.Thread(target=self._drain_tuner_log, daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _sweep_cmd(self) -> list[str]:
        """`rtl_power` over the range, one CSV row per bin-block per interval.

        rtl_power ONLY, and still deliberately — but on narrower grounds than before.
        `Dockerfile.sdr`'s rule is now APT ONLY, NO PIP rather than stdlib-only, so numpy
        and SoapySDR are in the image and an FFT of our own has become affordable; this
        stays rtl_power because nothing has replaced it yet, not because nothing could
        (docs/plans/SDR_IQ_SPECTRUM_PLAN.md F6 is where the swap happens). The rewrites
        are refused as they always were: `rtl_power_fftw` is a source build and
        `soapy_power` a pip install, and neither is in Debian.

        `-i 1` is one second per row, and MEASURED on this box that is also the real
        revisit: 1.0 s between consecutive readings of the same block at 1, 2, 4, 8 and
        22 hops alike, with every block carrying identical timestamps. rtl_power retunes
        WITHIN the interval rather than multiplying it. (This docstring used to claim the
        revisit was one second times the number of hops. It is not, anywhere in the range
        `MAX_SWEEP_SPAN_HZ` allows — and that cap is what bounds it: 60 MHz is ~25 hops,
        so 22 is close to the worst case a caller can ask for.)"""
        assert self.sweep is not None
        span = self.sweep
        cmd = [
            "rtl_power",
            "-f", f"{span.start_hz}:{span.stop_hz}:{span.bin_hz}",
            "-i", "1",
            "-e", str(int(span.seconds)),
        ]  # fmt: skip
        if self.gain:
            # Fixed gain, never AGC. A floor that moves with the signal makes dB values
            # incomparable across the sweep, and every threshold built on them drifts.
            cmd += ["-g", str(self.gain)]
        cmd += self._device_args()
        return [*cmd, self.sweep_csv]

    def _device_args(self) -> list[str]:
        """`-d <serial>`, or nothing at all.

        Nothing is not a neutral default once a second dongle exists: librtlsdr then
        opens whichever it enumerated first, which is a property of USB bus order rather
        than of anything the owner chose. Both tools take the same `-d` and both went
        without it, so the fix belongs in one place rather than twice.

        THE BARE SERIAL, not `serial=...`. rtl_fm and rtl_power hand `-d` straight to
        librtlsdr's `verbose_device_search`, which tries a raw index, then an exact
        serial, then a serial prefix, then a serial suffix — and has no key=value form
        at all. `serial=` is SoapySDR's syntax; passed here it matches nothing and the
        tool exits(1) before opening the device, which is WORSE than the bug it was
        meant to fix: the sidecar's `start` has already returned, so the lease looks
        live and the omnibox lights while nothing is decoding.

        Empty when the caller named no radio, which keeps a one-dongle box byte
        identical to what it ran before."""
        return ["-d", str(self.serial)] if self.serial else []

    def _start_sweep_pipeline(self) -> None:
        """One process, no threads, and it ENDS ON ITS OWN.

        rtl_power's `-e` is an exit timer, so the sweep terminates and `alive` goes
        false, at which point the tuner reaps the session and the radio frees itself.
        That is the difference from the other two purposes, and it is why a survey needs
        no stop from the caller — though Release still works, because the session is
        real and the Tuner owns it."""
        if shutil.which("rtl_power") is None:
            raise SdrError("rtl_power is not installed in this image")
        try:
            self._rtl = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                self._sweep_cmd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
        except OSError as exc:
            self._kill()
            raise SdrError(f"could not start the sweep: {exc}") from exc
        # Its stderr carries the retune plan and any device error, and an unread pipe
        # blocks the writer at 64 KB — the same hazard `_drain_tuner_log` exists for.
        self._threads = [threading.Thread(target=self._drain_tuner_log, daemon=True)]
        for thread in self._threads:
            thread.start()

    def _spectrum_cmd(self) -> list[str]:
        """The same rtl_power, with no exit timer and its CSV on stdout.

        `stdbuf -oL` because the whole feature turns on rtl_power flushing each row as
        it writes it. It does — `csv_dbm` ends in an `fflush` — but that is a property
        of a binary the image installs from apt rather than one this repo builds, and
        the failure mode if a future build ever changes it is a waterfall that paints
        nothing while every test still passes. Line buffering makes the guarantee ours.
        `stdbuf` execs its argument, so `self._rtl` is still rtl_power's own pid and
        `_kill` still reaches it.

        No `-e`: a live spectrum runs until the session is released, which is what makes
        it different from a survey (see PURPOSE_SPECTRUM)."""
        assert self.sweep is not None
        span = self.sweep
        cmd = ["stdbuf", "-oL", "rtl_power"] if _LINE_BUFFERED else ["rtl_power"]
        cmd += [
            "-f", f"{span.start_hz}:{span.stop_hz}:{span.bin_hz}",
            "-i", str(SPECTRUM_INTERVAL_S),
        ]  # fmt: skip
        if self.gain:
            # Fixed gain for the same reason the survey fixes it: a floor that moves
            # with the signal makes the colours mean nothing across the picture, and a
            # waterfall whose scale drifts is one nobody can read a weak signal off.
            cmd += ["-g", str(self.gain)]
        cmd += self._device_args()
        return [*cmd, "-"]

    def _start_spectrum_pipeline(self) -> None:
        """Whichever engine can draw this range: the I/Q one first, rtl_power behind it.

        The api names a one-hop capture when the band table has one, and that IS the
        engine choice — the band table lives in one place and the sidecar executes what
        it was handed (F6). A range too wide for one capture has no capture named, and
        rtl_power hops it exactly as before.

        **The fallback is a runtime one, and that is CLAUDE.md #10.** If the I/Q engine
        cannot open the radio on this box, an owner with no terminal must not need a
        revert and a rebuild to get a picture back — so a failure here drops to
        rtl_power for any range rtl_power can serve, and says so in the log. Only a
        range rtl_power cannot serve at all keeps the I/Q failure, because there the
        alternative is not a worse picture but a false one."""
        # Asked here as well as in `resweep` because this is the START path — reached
        # from `__init__`, where there is no session yet and nothing to lose by finding
        # out late. A retune cannot afford that, which is why the reading lives in one
        # function.
        unservable = spectrum_engine_refusal(self.sweep)
        if unservable is not None:
            raise SdrError(unservable)
        assert self.sweep is not None
        if self.sweep.capture is not None:
            try:
                self._start_iq_spectrum()
            except RadioUnavailable as failed:
                if self.sweep.start_hz < MIN_HZ:
                    raise
                print(  # noqa: T201
                    f"[spectrum] the I/Q engine would not start ({failed}); "
                    f"falling back to rtl_power for this session",
                    flush=True,
                )
            else:
                return
        if shutil.which("rtl_power") is None:
            raise SdrError("rtl_power is not installed in this image")
        try:
            self._rtl = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                self._spectrum_cmd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
        except OSError as exc:
            self._kill()
            raise SdrError(f"could not start the spectrum: {exc}") from exc
        self._threads = [
            threading.Thread(target=self._pump_spectrum, daemon=True),
            threading.Thread(target=self._drain_tuner_log, daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _start_iq_spectrum(self) -> None:
        """Open the radio ourselves and transform its samples here. One thread.

        No subprocess, so there is no CSV to parse, no `stdbuf` to trust and no hop
        plan on stderr — and no `_rtl` either, which is why teardown has to reach this
        handle explicitly rather than through `_kill`'s process loop."""
        assert self.sweep is not None and self.sweep.capture is not None
        rate_hz, bins = self.sweep.capture
        centre = self.sweep.centre_hz
        try:
            self._radio = radio.Radio.open(
                rate_hz=rate_hz,
                center_hz=centre,
                serial=self.serial,
                direct=centre < radio.DIRECT_MAX_HZ,
                doing=PURPOSE_LABEL[PURPOSE_SPECTRUM],
            )
        except radio.RadioBusy as busy:
            raise RadioUnavailable(str(busy)) from busy
        except radio.RadioError as failed:
            raise RadioUnavailable(str(failed)) from failed
        self._spectrometer = iq.Spectrometer(bins, rate_hz)
        self._threads = [threading.Thread(target=self._pump_iq_spectrum, daemon=True)]
        for thread in self._threads:
            thread.start()

    def _pump_iq_spectrum(self) -> None:
        """Raw I/Q -> our own FFT -> every viewer, as fast as the radio delivers.

        No interval and no sleep: `read` blocks until the samples exist, so the frame
        rate is the capture's own — `rate / (bins * segments)` — rather than a number
        this file picks. That is the whole point of owning the samples.

        A read that fails ENDS the session's picture rather than retrying forever: the
        radio is gone, or the stream stopped answering, and a waterfall that keeps
        painting the last row is worse than one that stops."""
        held, spectrometer = self._radio, self._spectrometer
        sweep = self.sweep
        if held is None or spectrometer is None or sweep is None:
            return
        try:
            if sweep.hops > 1:
                self._sweep_hops(held, spectrometer, sweep)
            else:
                self._stare(held, spectrometer)
        except (radio.RadioError, ValueError):
            pass  # a stop, or the radio went away; teardown is `_kill`'s job
        finally:
            if not self._restarting:
                self._end_frames()

    def _stare(self, held: "radio.Radio", spectrometer: "iq.Spectrometer") -> None:
        """One tuning, rows as fast as the frame budget allows."""
        want = spectrometer.n * segments_for(spectrometer.n, held.rate_hz)
        while not self._stopping and held.alive:
            reading = held.read(want)
            spectrum = spectrometer.frame(reading.samples, held.center_hz)
            self._publish_frame(
                Frame(
                    at=reading.at,
                    start_hz=int(spectrum.start_hz),
                    bin_hz=spectrum.bin_hz,
                    # Unrounded: `as_dict` is where the wire's one decimal is applied,
                    # and rounding twice just does the work twice while denying any
                    # other reader the precision it was handed.
                    db=spectrum.db_list(),
                )
            )

    def _sweep_hops(
        self, held: "radio.Radio", spectrometer: "iq.Spectrometer", sweep: Sweep
    ) -> None:
        """Several captures, retuned on the LIVE stream, stitched into one row.

        This is what `rtl_power` was doing and why it was slow — except its one row a
        second is not the cost of hopping, it is `if (interval < 1) interval = 1;` in
        its own C. Here the cost is real and visible: each hop pays a retune barrier,
        and the samples are the cheap part.

        Each hop contributes only the TRUSTED MIDDLE of its capture, because a
        capture's edges sit in the tuner's IF rolloff and drawing them puts a dip at
        every hop seam — the picture then shows the receiver's own filter shape as if
        it were the band. The centres are derived from the same arithmetic as the
        placement, so a hop cannot own bins it was not tuned for."""
        assert sweep.capture is not None
        rate_hz, bins = sweep.capture
        usable = hop_usable_bins(bins)
        edge = (bins - usable) // 2
        segments = HOP_SEGMENTS
        want = bins * segments
        row = np.full(usable * sweep.hops, iq.DB_FLOOR, dtype=np.float64)
        centres = hop_centres(sweep.start_hz, rate_hz, bins, sweep.hops)
        while not self._stopping and held.alive:
            at = 0.0
            for index, centre in enumerate(centres):
                if self._stopping or not held.alive:
                    return
                if held.center_hz != centre:
                    held.retune(center_hz=centre)
                reading = held.read(want)
                at = at or reading.at
                spectrum = spectrometer.frame(reading.samples, centre)
                row[index * usable : (index + 1) * usable] = spectrum.db[
                    edge : edge + usable
                ]
            self._publish_frame(
                Frame(
                    at=at,
                    start_hz=sweep.start_hz,
                    bin_hz=spectrometer.bin_hz,
                    db=row.tolist(),
                )
            )

    def _pump_spectrum(self) -> None:
        """rtl_power's CSV -> whole frames -> every viewer."""
        rtl = self._rtl
        if rtl is None or rtl.stdout is None:
            return
        stitch = Stitch()
        try:
            for raw in rtl.stdout:
                if self._stopping:
                    break
                for frame in stitch.push(raw.decode("utf-8", "replace")):
                    self._publish_frame(frame)
        except (ValueError, OSError):
            pass  # a stop, or the process went away; _kill handles teardown
        finally:
            # A retune replaces the process under viewers who stay attached, and each
            # frame says which band it covers — so they need no sentinel and no reconnect
            # to follow the move. Only a real end closes their streams.
            if not self._restarting:
                self._end_frames()

    def _publish_frame(self, frame: Frame) -> None:
        # Signals found HERE rather than at the three places a frame is built: both
        # engines and the stitcher pass through this one seam, so no path can publish a
        # row whose peaks nobody looked for — and a viewer cannot disagree with the
        # agent about what was on the air, because neither of them decides.
        frame = dataclasses.replace(
            frame,
            peaks=peaks.find(
                frame.db,
                frame.start_hz,
                frame.bin_hz,
                channel_hz=self.sweep.channel_hz if self.sweep else 0,
            ),
        )
        with self._lock:
            self._last = frame
            subs = list(self._frames)
        for sub in subs:
            try:
                sub.put_nowait(frame)
            except queue.Full:
                pass  # a waterfall row is worthless late; drop for that viewer only

    def _end_frames(self) -> None:
        with self._lock:
            subs = list(self._frames)
            self._frames.clear()
        for sub in subs:
            try:
                sub.put_nowait(None)  # sentinel: the stream ended
            except queue.Full:
                pass

    def subscribe_frames(self) -> queue.Queue[Frame | None]:
        """Attach one viewer, seeded with the most recent row if there is one."""
        sub: queue.Queue[Frame | None] = queue.Queue(maxsize=SPECTRUM_QUEUE)
        with self._lock:
            self._frames.add(sub)
            last = self._last
        if last is not None:
            with contextlib.suppress(queue.Full):
                sub.put_nowait(last)
        return sub

    def unsubscribe_frames(self, sub: queue.Queue[Frame | None]) -> None:
        with self._lock:
            self._frames.discard(sub)

    @property
    def refusal(self) -> str:
        """The line where the tool said it could not have the radio, if it said so.

        The one line worth putting in front of a person. The rest of the tail is setup
        chatter — hop plans and buffer sizes — and leading with it buries the sentence
        that names what to fix."""
        for line in list(self._tail):
            if any(sig in line.lower() for sig in _CANNOT_OPEN):
                return line
        return ""

    def _confirm_started(self) -> None:
        """Refuse a session whose radio could not be opened.

        Without this, `start` answers 200 for a pipeline that cannot work: the api
        reports a session, the omnibox lights, and a moment later the tuner reaps a dead
        one with nothing anywhere saying why. The only trace is a line in a container
        log, which is precisely what the owner cannot read (CLAUDE.md #10).

        MEASURED on the box 2026-09-04. A dongle whose USB descriptors had stopped
        answering enumerated blank, and every `-d <serial>` lookup failed. **The tool did
        not exit on it** — `rtl_power` printed `No matching devices found` and carried on
        printing its hop plan, alive well past this grace — so waiting for a process to
        die catches only the fast half. What it always does is SAY so, which is what
        `_CANNOT_OPEN` matches: faster than an exit and not a guess about timing.

        The liveness check stays as the general case, for a pipeline that dies without
        an explanation this list knows."""
        deadline = time.time() + STARTUP_GRACE_S
        said = ""
        while time.time() < deadline:
            said = self.refusal
            if said or not self.alive:
                break
            time.sleep(0.02)
        if not said and self.alive:
            return
        # Give the log drain the moment it needs to have read the tool's complaint. On
        # the exit path the process is already gone, so this is a read of a closed pipe.
        for thread in self._threads:
            thread.join(timeout=0.2)
        said = said or self.refusal or self.tail or "it exited without saying why"
        self._released = True
        self._stopping = True
        self._kill()
        raise SdrError(f"the radio did not start: {said}")

    def _start_packet_pipeline(self) -> None:
        """rtl_fm -> direwolf. No encoder, because there is no audio to serve.

        Direwolf does the hard half — bit sync, NRZI, HDLC, the CRC — and hands whole
        frames over a KISS socket, which `packets.py` unwraps. It reads audio on stdin
        and must NEVER see EOF: end of input ends its session, so the pipe stays open
        for the life of the lease exactly as rtl_fm keeps it fed.

        The KISS reader attaches once and stays attached. Measured against a real
        capture: direwolf forwards frames only to clients ALREADY connected, so a
        reader that reconnects loses whatever arrived in the gap — a hole in the log
        rather than something that can be backfilled."""
        if shutil.which("rtl_fm") is None:
            raise SdrError("rtl_fm is not installed in this image")
        if shutil.which("direwolf") is None:
            raise SdrError("direwolf is not installed in this image")
        try:
            self._rtl = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                self._rtl_cmd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            self._enc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                self._direwolf_cmd(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            self._kill()
            raise SdrError(f"could not start the packet pipeline: {exc}") from exc

        self._threads = [
            threading.Thread(target=self._pump_pcm, daemon=True),
            threading.Thread(target=self._read_packets, daemon=True),
            threading.Thread(target=self._drain_tuner_log, daemon=True),
            # Direwolf's own stdout MUST be read. It writes ~64 lines at startup and
            # more per packet, and an unread pipe blocks its writer at 64 KB — which
            # would stop it decoding, for ever, with the session still reporting
            # healthy. Exactly the hazard _drain_tuner_log documents for rtl_fm.
            threading.Thread(target=self._drain_decoder_log, daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _direwolf_cmd(self) -> list[str]:
        """Direwolf reading raw PCM on stdin at the tuner's rate, KISS on a local port.

        `-t 0` kills the ANSI colour it otherwise writes into the container log.

        `-q d`, NOT `-q hd`. `h` means precisely "suppress the heard line with the audio
        level", and that line is the ONLY place direwolf reports how strong a
        transmission was — suppressing it is what made signal level look unrecoverable.
        `d` still drops the per-packet decode dump. MEASURED on direwolf 1.7, `hd` only
        took 67 lines to 64 across three packets, so it was never what kept the output
        small and never why the pipe is safe. `_drain_decoder_log` is why."""
        return [
            "direwolf",
            "-c",
            self._direwolf_conf(),
            "-t",
            "0",
            "-q",
            "d",
            "-r",
            str(AUDIO_RATE),
            "-B",
            "1200",
            "-",
        ]

    def _direwolf_conf(self) -> str:
        """Write direwolf's config beside the session and return its path.

        A file rather than flags because ADEVICE/KISSPORT have no command-line form.
        Written per session so a retune or a restart cannot inherit a stale port."""
        conf = f"""ADEVICE stdin null
ACHANNELS 1
CHANNEL 0
MODEM 1200
AGWPORT 0
KISSPORT {self.kiss_port}
"""
        path = f"/tmp/direwolf-{self.id}.conf"  # noqa: S108 - container-local, per session
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(conf)
        return path

    def _read_packets(self) -> None:
        """Hold one KISS connection for the life of the session and fan frames out.

        Retries the connect because direwolf binds its port a moment after launch;
        after that a drop is not retried in a loop, since reconnecting cannot recover
        what was missed and a tight loop against a dead process is just noise."""
        stream = packets.KissStream()
        sock = None
        for _ in range(40):
            if self._stopping:
                return
            try:
                sock = socket.create_connection(
                    ("127.0.0.1", self.kiss_port), timeout=2
                )
                break
            except OSError:
                time.sleep(0.25)
        if sock is None:
            # Reporting healthy while decoding nothing is the failure mode this whole
            # wave is meant not to have. Killing the pipeline makes `alive` false, so
            # the tuner reaps the session and the radio reads as idle — visibly wrong
            # rather than invisibly deaf.
            print(  # noqa: T201
                f"[direwolf] no KISS connection on {self.kiss_port}; ending the session",
                flush=True,
            )
            self._kill()
            return
        try:
            sock.settimeout(1.0)
            while not self._stopping:
                try:
                    chunk = sock.recv(4096)
                except TimeoutError:
                    continue
                if not chunk:
                    return
                for packet in stream.feed(chunk):
                    # `replace` rather than assignment: a Packet is frozen, which is
                    # what makes it safe to fan one out to every subscriber.
                    level = self._take_audio_level()
                    if level is not None:
                        packet = dataclasses.replace(packet, audio_level=level)
                    self._publish_packet(packet)
        except OSError:
            return
        finally:
            sock.close()

    def _drain_decoder_log(self) -> None:
        """Read direwolf's stdout forever, into the container log.

        Not optional and not for diagnostics: an unread pipe blocks its writer at
        64 KB. Direwolf writes ~64 lines before it decodes anything and more per packet,
        so an undrained pipe stops it decoding permanently while the session goes on
        reporting healthy — the same failure `_drain_tuner_log` exists to prevent."""
        enc = self._enc
        if enc is None or enc.stdout is None:
            return
        try:
            for line in enc.stdout:
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                level = packets.parse_audio_level(text)
                if level is not None:
                    with self._lock:
                        self._level = (time.monotonic(), level)
                # Still printed, every line. Parsing must not turn the drain into a
                # filter: an unread pipe is what blocks direwolf at 64 KB.
                print(f"[direwolf] {text}", flush=True)  # noqa: T201
        except (ValueError, OSError):
            pass  # the process went away; teardown handles it

    def _take_audio_level(self) -> int | None:
        """The level direwolf announced for THIS frame, or None.

        Direwolf hands the level over on stdout and the frame over a KISS socket, so
        this is a correlation between two streams rather than one field on one record.
        What was measured (see `packets.parse_audio_level`) is that the two arrive in
        the same millisecond, 1:1 and in order, and that a failed decode prints no level
        at all — so pairing by ORDER holds, and pairing by callsign would not, because
        the log line names the digipeater on a relayed frame.

        Consumed once, and expired fast. The failure this rules out is a level from an
        earlier transmission silently attaching to a later one: a plausible wrong number
        is worse than a blank, because nothing else on screen would contradict it.

        The frame thread can win the race against the log thread, so a frame may find
        nothing waiting and report None. That costs the number on an occasional row; it
        never puts the wrong number on any row."""
        with self._lock:
            pending = self._level
            self._level = None
        if pending is None:
            return None
        when, level = pending
        return level if time.monotonic() - when <= _LEVEL_WINDOW_S else None

    def _publish_packet(self, packet: packets.Packet) -> None:
        """Hand one decoded frame to every attached reader, dropping for a slow one.

        Same backpressure rule as the audio subscribers: a reader that stopped reading
        must not wedge the decoder for everyone else."""
        with self._lock:
            queues = list(self._packets)
        for queue_ in queues:
            # Make room and RETRY. Catching Full and dropping the oldest without
            # retrying discards the packet being published — measured, that lost every
            # other frame while a reader lagged. This is a LOG: late still counts.
            while True:
                try:
                    queue_.put_nowait(packet)
                    break
                except queue.Full:
                    try:
                        queue_.get_nowait()
                    except queue.Empty:
                        break

    def subscribe_packets(self) -> queue.Queue[packets.Packet | None]:
        sub: queue.Queue[packets.Packet | None] = queue.Queue(maxsize=PACKET_QUEUE)
        with self._lock:
            self._packets.add(sub)
        return sub

    def unsubscribe_packets(self, sub: queue.Queue[packets.Packet | None]) -> None:
        with self._lock:
            self._packets.discard(sub)

    def _drain_tuner_log(self) -> None:
        """Read rtl_fm's stderr, forever, into the container log.

        Two reasons, and the first is not optional: an unread pipe fills at 64 KB and
        then BLOCKS the writer, so a chatty tuner would wedge itself mid-session with
        no way to tell from outside. The second is that when the radio misbehaved
        there was nowhere to look — rtl_fm's own account of what it tuned and how it
        is coping never reached `docker logs`. Now it does."""
        rtl = self._rtl
        if rtl is None or rtl.stderr is None:
            return
        try:
            for line in rtl.stderr:
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self._tail.append(text)
                    print(f"[rtl_fm] {text}", flush=True)  # noqa: T201
        except (ValueError, OSError):
            pass  # the process went away; teardown handles it

    def _seg_seconds(self) -> float:
        """Seconds of audio held in the open segment (16-bit mono)."""
        return sum(len(part) for part in self._seg) / 2 / AUDIO_RATE

    def _cut(self, force: bool = False) -> None:
        """Close the open segment and queue it, if it is worth transcribing.

        The SQUELCH lives here, at the audio, rather than in the caller: rtl_fm with no
        squelch emits loud hiss into an empty channel, and whisper answers noise with
        fluent invented sentences (the capture route already warns about exactly this).
        A segment whose loudest moment never crosses the floor is dropped and never
        leaves this process, so a quiet frequency produces no captions instead of
        confident fiction."""
        pcm = b"".join(self._seg)
        self._seg.clear()
        self._seg_peak = 0.0
        started = self._seg_started
        self._seg_started = time.time()
        if not pcm or len(pcm) < AUDIO_RATE:  # under half a second is not speech
            return
        if not force and self._seg_peak_seen < SEGMENT_SQUELCH:
            self._seg_peak_seen = 0.0
            return
        self._seg_peak_seen = 0.0
        with self._lock:
            queues = list(self._segments)
        for queue_ in queues:
            # Drop the OLDEST to make room, never the newest. `put_nowait` on a full
            # queue raises and discards what you were adding, which is exactly backwards
            # for live captions: a captioner that fell behind would then work forever
            # through stale audio while every fresh segment was thrown away, and the lag
            # would never close. Captions are worthless late, so the backlog gives way.
            while True:
                try:
                    queue_.put_nowait((started, pcm))
                    break
                except queue.Full:
                    try:
                        queue_.get_nowait()
                    except queue.Empty:
                        break

    def _accumulate(self, chunk: bytes, level: float) -> None:
        """Grow the open segment, and close it on a gap or at the ceiling.

        Cutting on a QUIET GAP rather than a fixed clock is what keeps words whole:
        a boundary through the middle of a word garbles both sides of it, and a voice
        channel hands us natural gaps between transmissions to cut on instead."""
        if not self._segments:
            return  # nobody is captioning; do not accumulate audio nobody will read
        self._seg.append(chunk)
        self._seg_peak_seen = max(self._seg_peak_seen, level)
        # Measured in AUDIO, not wall clock. They coincide while the pipeline runs in
        # real time, but the length that matters to whisper is how much sound the
        # segment holds — and a stalled or bursting pipeline makes the clock lie.
        held = self._seg_seconds()
        if held >= SEGMENT_MAX_S:
            self._cut()
            return
        # A gap only ends a segment once there is enough audio to be worth sending.
        if held >= SEGMENT_MIN_S and level < SEGMENT_GAP_LEVEL:
            self._quiet_for += 1
            if self._quiet_for >= SEGMENT_GAP_CHUNKS:
                self._cut()
        else:
            self._quiet_for = 0

    def subscribe_segments(self) -> queue.Queue[tuple[float, bytes]]:
        """Start segmenting for one captioner. Accumulation only runs while subscribed."""
        sub: queue.Queue[tuple[float, bytes]] = queue.Queue(maxsize=SEGMENT_QUEUE)
        with self._lock:
            self._segments.add(sub)
        return sub

    def unsubscribe_segments(self, sub: queue.Queue[tuple[float, bytes]]) -> None:
        with self._lock:
            self._segments.discard(sub)
            idle = not self._segments
        if idle:
            self._seg.clear()
            self._seg_peak_seen = 0.0

    def _pump_pcm(self) -> None:
        """rtl_fm -> (measure) -> ffmpeg. The tap that makes the level meter honest."""
        rtl, enc = self._rtl, self._enc
        if rtl is None or rtl.stdout is None or enc is None or enc.stdin is None:
            return
        try:
            while not self._stopping:
                chunk = rtl.stdout.read(_CHUNK)
                if not chunk:
                    break
                level = _peak(chunk)
                self.audio_peak = level
                self._accumulate(chunk, level)
                enc.stdin.write(chunk)
                enc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass  # a stop, or the encoder went away; _kill handles teardown
        finally:
            try:
                if enc.stdin is not None:
                    enc.stdin.close()
            except OSError:
                pass

    def _pump_audio(self) -> None:
        """ffmpeg -> every subscriber. A slow subscriber is dropped, not waited on."""
        enc = self._enc
        if enc is None or enc.stdout is None:
            return
        try:
            while not self._stopping:
                chunk = enc.stdout.read(_CHUNK)
                if not chunk:
                    break
                with self._lock:
                    subs = list(self._subs)
                for sub in subs:
                    try:
                        sub.put_nowait(chunk)
                    except queue.Full:
                        pass  # live audio is worthless late
        except (ValueError, OSError):
            pass
        finally:
            # Only when the SESSION is over. On a retune the listeners keep their
            # connections and pick up the new pipeline's frames mid-stream, which is
            # exactly what MP3's self-framing buys us.
            if not self._restarting:
                with self._lock:
                    subs = list(self._subs)
                for sub in subs:
                    try:
                        sub.put_nowait(None)  # sentinel: the stream ended
                    except queue.Full:
                        pass

    def _kill(self) -> None:
        """SIGTERM, then SIGKILL only if it will not go.

        Not politeness: `rtl_fm` and `rtl_power` install a handler that cancels the
        pending async USB transfer and CLOSES THE DEVICE. SIGKILL never runs it, so the
        RTL2832U is left with transfers submitted, and a device torn down that way can
        stop answering the descriptor reads librtlsdr does to enumerate it — after which
        every `-d <serial>` lookup fails and the radio looks absent while sysfs still
        lists it. Two seconds is far more than the handler needs; the kill is still
        there for a tool that has genuinely wedged."""
        for proc in (self._rtl, self._enc):
            if proc is None:
                continue
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    proc.kill()
                    proc.wait(timeout=3)
            except OSError:
                pass
            if proc.poll() is None:
                # SIGKILL did not take (a process wedged in a USB ioctl is the way that
                # happens here). Dropping the handle on the next line would make it
                # unreachable for the life of the container while it still holds the
                # radio, so park it and let `reap_survivors` keep trying.
                _park(proc)
        self._rtl = self._enc = None
        held, self._radio, self._spectrometer = self._radio, None, None
        if held is not None:
            # A close that FAILS keeps its registry entry inside `radio.py`, so the
            # handle stays discoverable and `/reset` still refuses rather than firing a
            # port reset under it. Nothing here can act on the failure, and raising
            # would abort a teardown with a direwolf config still to unlink.
            with contextlib.suppress(radio.RadioError):
                held.close()
        # The per-session direwolf config would otherwise accumulate in /tmp for the
        # life of the container, one file per lease taken.
        with contextlib.suppress(OSError):
            os.unlink(f"/tmp/direwolf-{self.id}.conf")  # noqa: S108 - written by this session

    # ---- public surface -------------------------------------------------------

    def tune(self, frequency_hz: int, mode: str | None = None) -> None:
        """Retune in place. Restarts the pipeline but keeps the session id, its
        listeners, and — through `_restart` — its refusal to relaunch once released."""
        wanted = validate(frequency_hz, mode or self.mode)

        def apply() -> None:
            self.frequency_hz = frequency_hz
            self.mode = wanted
            self.audio_peak = 0.0

        self._restart(apply)

    def resweep(self, sweep: Sweep) -> None:
        """Point a live spectrum at a different range, in place.

        The same move `tune` makes, and it has to be a separate entry point rather than
        a wider `tune` because the two carry different things: a tuner is told one
        frequency and a mode, a waterfall is told a span and a bin width, and there is
        no reading of "frequency_hz" that means both. `frequency_hz` follows the new
        centre so the omnibox keeps naming something true.

        Viewers stay attached across it and are told nothing: every frame says which
        band it covers, so the picture simply starts describing the new one.

        **Everything that can refuse is asked BEFORE `_restart`, which destroys first.**
        `_restart` kills the pipeline and applies the new range before it relaunches, so
        a check that fires from inside the relaunch fires on a session whose old picture
        is already dead and whose `sweep` already names the range that cannot be served
        — `alive` then goes false and the next `Tuner._reap` stops the session and drops
        the lease. The owner tapped a band they were told they could have and lost the
        waterfall AND the radio. Refusing up here leaves the old picture running."""
        if self.purpose not in SWEEPING:
            raise SdrError(f"a {self.purpose} session has no range to move")
        unservable = spectrum_engine_refusal(sweep)
        if unservable is not None:
            raise SdrError(unservable)

        def apply() -> None:
            self.sweep = sweep
            self.frequency_hz = sweep.centre_hz
            self._last = None

        self._restart(apply)

    def _restart(self, apply: Callable[[], None]) -> None:
        """Tear the pipeline down, apply the new tuning, bring it back up.

        Refuses once the session has been released, and this is the load-bearing half.
        The route resolves the Session under the tuner's lock and calls this OUTSIDE it,
        so a `/listen/stop` landing in between used to make `_start_pipeline` spawn a
        fresh `rtl_fm` for a session no longer in `_sessions`: invisible to
        `blocking_key`, never reaped, and holding the dongle until the container
        restarts — after which the next caller for that serial is allowed through and
        two processes fight over one radio. That is the "garbled audio rather than an
        error" outcome the whole per-radio rule exists to prevent, so the guard belongs
        here rather than in the route, where the window is.

        The same ordering is why VALIDATION cannot live in here, and callers must do it
        first: by the time `_start_pipeline` runs, `_kill` has already happened and
        `apply` has already written the new tuning, so anything this raises kills a
        session that was working rather than refusing a request that was not. `tune`
        validates through `validate()` and `resweep` through `spectrum_engine_refusal`,
        both above the call — the guard here is about a session RELEASED underneath us,
        which is the one thing no caller can check in advance."""
        with self._lock:
            if self._released:
                raise SessionGone("that session has been released")
            self._restarting = True
        self._stopping = True
        try:
            self._kill()
            for thread in self._threads:
                thread.join(timeout=2)
            self._stopping = False
            apply()
            self._start_pipeline()
            # The guard above is not enough on its own, and this is where it showed.
            # `stop()` takes the lock, sets `_released`, and kills the processes it can
            # see; land it in the window between that guard and this line and it kills
            # NOTHING (the old pipeline is already down, the new one not yet up), the
            # session leaves `_sessions`, and the pipeline spawned a moment later holds
            # the dongle with nothing anywhere still pointing at it. MEASURED on the box
            # 2026-09-04: an `rtl_fm` alive for hours, `[rtl_fm] Error: dropped samples`
            # on repeat because `_stopping` was already true when its pumps started so
            # nothing drained its stdout, and `/dev/bus/usb/001/011` claimed by usbfs
            # while the PWA said "Idle — nothing is holding this radio".
            with self._lock:
                abandoned = self._released
            if abandoned:
                self._stopping = True
                self._kill()
                raise SessionGone("that session has been released")
            # A retune onto a radio that has stopped answering fails the same way a
            # start does, and the route turns it into the same sentence rather than a
            # session that quietly disappears a second later.
            self._confirm_started()
        finally:
            # Cleared only after the pumps have exited and the new ones are up, so the
            # old pump's teardown sees it set and keeps every listener attached. If the
            # relaunch raises, clearing it here means a later stop() still says so.
            self._restarting = False

    def subscribe(self) -> queue.Queue[bytes | None]:
        sub: queue.Queue[bytes | None] = queue.Queue(maxsize=_SUB_QUEUE_CHUNKS)
        with self._lock:
            self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: queue.Queue[bytes | None]) -> None:
        with self._lock:
            self._subs.discard(sub)

    def stop(self) -> None:
        # Set BEFORE the kill and under the lock, so a `tune` racing this either sees it
        # and refuses, or has already passed its own check and is holding the lock we
        # are waiting for. Either order leaves exactly one of them relaunching.
        with self._lock:
            self._released = True
        self._stopping = True
        self._kill()
        with self._lock:
            subs = list(self._subs)
            self._subs.clear()
            packet_subs = list(self._packets)
            self._packets.clear()
        for sub in subs:
            try:
                sub.put_nowait(None)
            except queue.Full:
                pass
        # Packet readers need the same end-of-stream sentinel the audio ones get.
        # Without it a released session left every reader blocked for ever, still
        # emitting keep-alives the api reads as "logging is healthy" — and pinning the
        # dead Session and a server thread apiece.
        for sub in packet_subs:
            try:
                sub.put_nowait(None)
            except queue.Full:
                pass
        # ...and so do waterfall viewers, for the same reason: a released spectrum
        # session that never closed its streams would hold a server thread apiece,
        # each still reporting a healthy picture of a radio nothing is watching.
        self._end_frames()

    @property
    def tail(self) -> str:
        """The tool's own last words, for a refusal that has to name a cause.

        A LIVE read of the deque, so it carries whatever had been printed by the time
        the caller asked — which for a pipeline that died on the spot is the whole
        complaint, librtlsdr's device list included."""
        return "; ".join(self._tail)

    @property
    def alive(self) -> bool:
        """Whether this session can still do its job.

        BOTH processes, not just the tuner. A logging session whose direwolf died —
        crashed, failed to bind, or wedged — was still reporting a healthy `aprs` lease
        while decoding nothing, and the owner's only clue would have been a log that
        stopped growing. `current()` reaps a dead session, so this is what turns a
        silent death into an idle radio the owner can see."""
        # The I/Q engine holds a radio instead of running a process, so there is no
        # `poll()` to ask and the process test would call a healthy session dead — after
        # which `current()` reaps it and the waterfall it is feeding disappears. What
        # "still doing its job" means there is the open device (F6).
        if self._radio is not None:
            return self._radio.alive
        if self._rtl is None or self._rtl.poll() is not None:
            return False
        return self._enc is None or self._enc.poll() is None

    def info(self) -> SessionInfo:
        with self._lock:
            listeners = len(self._subs)
        return SessionInfo(
            session_id=self.id,
            frequency_hz=self.frequency_hz,
            mode=self.mode,
            gain=self.gain,
            started_at=self.started_at,
            audio_peak=self.audio_peak,
            listeners=listeners,
            purpose=self.purpose,
            serial=self.serial,
            sweep=self.sweep.as_dict() if self.sweep is not None else None,
        )


class Tuner:
    """Every session the box is holding, one per RADIO.

    Was one slot, which is what made APRS logging and the tuner take turns on a box with
    two dongles plugged in. Now keyed by serial, so a service on the long wire and the
    tuner on the desk whip run at once and the 409 is per device rather than global.

    **An UNNAMED session conflicts with everything.** A session started with no serial
    opens whichever device librtlsdr enumerates first, so nothing can prove it is not on
    the radio a second caller is asking for. Refusing is the only honest answer: the
    alternative is two processes fighting over one dongle, which fails as garbled audio
    rather than as an error. In practice the api resolves a serial whenever it can see
    the USB scan, so this is the one-dongle box — where one session was always the
    limit — and the scan-unreachable case, where caution is the point.
    """

    #: The key an unnamed session takes; see `blocking_key`, which the capture path in
    #: server.py shares so the two holders of a radio cannot disagree about who blocks.
    ANY = ANY_DEVICE

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        # Radios held by something that is NOT a session: the one-shot capture path,
        # which runs rtl_fm to completion rather than keeping a pipeline. Key → what it
        # is doing and when the claim lapses. Here rather than behind its own lock in
        # server.py because that is what it WAS, and a capture could then start on a
        # radio a session held only because capture checked first — while a session
        # could start on a radio a capture held, because `start` never checked at all.
        # One registry under one lock makes both directions true and neither racy.
        self._reserved: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def _reap(self) -> None:
        """Drop sessions whose pipeline died (device unplugged, driver reclaimed it).

        Caller holds the lock. A dead session must not hold a radio against the next
        caller, and must not be reported as live."""
        for key, session in list(self._sessions.items()):
            if not session.alive:
                session.stop()
                self._sessions.pop(key, None)
        # Nothing to do with sessions, and deliberately on the same beat: a parked
        # process is one holding a radio that every `blocking_key` here believes is
        # free, so the sweep that decides what is free is exactly when to retry it.
        reap_survivors()

    def _holders(self) -> dict[str, str]:
        """Every held radio → what is holding it, in the words a refusal uses. Caller
        holds the lock.

        Lapsed reservations are dropped here rather than trusted. A session is reaped by
        asking its process whether it is alive; a reservation has no process to ask —
        it is a claim staked by a `capture` that promises to release it in a `finally`.
        A promise is not a reap: a signal between taking the claim and entering that
        `try`, or a worker thread killed outright, strands the key for the lifetime of
        the process, and `blocking_key` then refuses that radio for ever (every radio,
        if the claim was unnamed) while `/healthz` reports `busy` with nothing running.
        The deadline is what a capture can legally take, so an expiry is always a leak
        rather than a slow caller."""
        now = time.monotonic()
        for key, (_doing, expires) in list(self._reserved.items()):
            if expires <= now:
                self._reserved.pop(key, None)
        held = {
            key: PURPOSE_LABEL.get(s.purpose, "in use") for key, s in self._sessions.items()
        }
        held.update({key: doing for key, (doing, _e) in self._reserved.items()})
        return held

    def _blocker(self, key: str) -> tuple[str, str] | None:
        """The radio key that stops `key` from being taken, and what holds it. Caller
        holds the lock."""
        held = self._holders()
        hit = blocking_key(held, key)
        return None if hit is None else (hit, held[hit])

    def _busy(self, key: str) -> SdrBusy | None:
        """The refusal to raise for `key`, or None if it is free. Caller holds the
        lock."""
        blocker = self._blocker(key)
        if blocker is None:
            return None
        which = f" ({blocker[0]})" if blocker[0] else ""
        return SdrBusy(f"the radio{which} is already {blocker[1]}")

    def reserve(self, serial: str | None, doing: str) -> SdrBusy | None:
        """Hold a radio for something that is not a session; None means it is yours.

        Returns the refusal rather than a bool so the caller never has to ask a second
        time what blocked it — between the two questions the answer can change, and a
        message naming the wrong holder is worse than no message.

        Never blocks: a caller waiting an unknown time on a radio someone else is using
        is worse than a plain no (docs/plans/SDR_RADIO_PLAN.md §4.2)."""
        key = serial or self.ANY
        with self._lock:
            self._reap()
            busy = self._busy(key)
            if busy is None:
                self._reserved[key] = (doing, time.monotonic() + RESERVATION_TTL_S)
            return busy

    def unreserve(self, serial: str | None) -> None:
        with self._lock:
            self._reserved.pop(serial or self.ANY, None)

    def reserved(self) -> bool:
        """Whether anything holds a radio outside a session — `/healthz`'s `busy`.

        Through `_holders` so a lapsed claim is dropped first: `busy` stuck on with
        nothing running is the visible half of the leak `_holders` describes."""
        with self._lock:
            self._holders()  # expires lapsed claims
            return bool(self._reserved)

    def start(
        self,
        frequency_hz: int,
        mode: str,
        gain: str | None,
        purpose: str = PURPOSE_LISTEN,
        sweep: Sweep | None = None,
        serial: str | None = None,
    ) -> SessionInfo:
        validate_purpose(purpose)
        key = serial or self.ANY
        with self._lock:
            self._reap()
            busy = self._busy(key)
            if busy is not None:
                raise busy
            session = Session(frequency_hz, mode, gain, purpose, sweep, serial)
            self._sessions[key] = session
            return session.info()

    def sessions(self) -> list[Session]:
        """Every live session, in serial order so the list is stable to read."""
        with self._lock:
            self._reap()
            return [self._sessions[k] for k in sorted(self._sessions)]

    #: How the omnibox's ONE icon chooses between live sessions: prefer what a person is
    #: most likely asking about — the tuner they opened — then a service, then a sweep.
    #: Serial only BREAKS A TIE, so the answer is deterministic without being arbitrary.
    _SHOWN_FIRST = {
        PURPOSE_LISTEN: 0,
        PURPOSE_APRS: 1,
        PURPOSE_SPECTRUM: 2,
        PURPOSE_SURVEY: 3,
    }

    @classmethod
    def _worth_showing(cls, live: list[Session]) -> Session | None:
        """The one to draw, out of a snapshot the caller already has."""
        if not live:
            return None
        rank = len(cls._SHOWN_FIRST)
        return min(live, key=lambda s: (cls._SHOWN_FIRST.get(s.purpose, rank), s.serial or ""))

    def current(self, serial: str | None = None) -> Session | None:
        """One session: the named radio's, or the one worth showing.

        Deterministic rather than arbitrary, because "which session is showing" must not
        change between two reads that changed nothing."""
        live = self.sessions()
        if serial is not None:
            return next((s for s in live if s.serial == serial), None)
        return self._worth_showing(live)

    def snapshot(self) -> tuple[Session | None, list[Session]]:
        """Every live session, and the one worth showing, from ONE reading.

        `/healthz` reports both, and taking them from two calls let a session appear in
        `sessions` but not in `listening` — or the reverse — for callers that reasonably
        assume `listening` is one of `sessions`. `health.session_for`'s fallback assumes
        exactly that."""
        live = self.sessions()
        return self._worth_showing(live), live

    def find(self, session_id: str) -> Session | None:
        """The session with this id, whichever radio it is on."""
        return next((s for s in self.sessions() if s.id == session_id), None)

    def for_purpose(self, purpose: str) -> Session | None:
        """The session holding a radio for this job — what a purpose-specific route
        (packets, captions) needs now that "the session" is no longer one thing."""
        return next((s for s in self.sessions() if s.purpose == purpose), None)

    def stop(self, session_id: str | None = None) -> bool:
        """Release one session, or every session when no id is given.

        No id used to mean "the one"; with several it has to mean ALL, because the
        alternative — releasing an arbitrary one — is a control that does something
        different each time it is pressed."""
        with self._lock:
            self._reap()
            if session_id is None:
                if not self._sessions:
                    return False
                for session in list(self._sessions.values()):
                    session.stop()
                self._sessions.clear()
                return True
            for key, session in list(self._sessions.items()):
                if session.id == session_id:
                    session.stop()
                    self._sessions.pop(key, None)
                    return True
            return False
