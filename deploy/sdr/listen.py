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
from dataclasses import dataclass
from typing import Any

import packets

MIN_HZ = 24_000_000
MAX_HZ = 1_766_000_000
MODES = {
    "fm": "fm",
    "nfm": "fm",
    "wbfm": "wbfm",
    "am": "am",
    "usb": "usb",
    "lsb": "lsb",
}

# What a session is HOLDING the tuner for. One radio, so the lease is the only arbiter,
# and which job holds it decides what the loser is told: "release it to listen" and
# "release it to log" are opposite advice (docs/plans/APRS_CONTROL_PLAN.md P0).
# `listen` produces audio; `aprs` decodes packets and produces none.
PURPOSE_LISTEN = "listen"
PURPOSE_APRS = "aprs"
# A band sweep. Unlike the other two it ENDS ON ITS OWN when rtl_power's exit timer
# fires, so the radio frees itself — but it is still a real Session while it runs, which
# is the whole point: the omnibox icon, the elapsed clock, Release and the 409 all read
# the session's existence, so a sweep that held the radio outside the lease would be a
# radio held by something invisible.
PURPOSE_SURVEY = "survey"
# Every purpose maps to the phrase a refusal uses. `.get` rather than `[]` because this
# is read while holding the tuner lock on the contention path: a purpose added without a
# label would otherwise raise KeyError there and turn a 409 into a 500 with a traceback.
PURPOSE_LABEL = {
    PURPOSE_LISTEN: "listening",
    PURPOSE_APRS: "logging APRS",
    PURPOSE_SURVEY: "sweeping the band",
}
PURPOSES = tuple(PURPOSE_LABEL)

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
    """This radio is already held. One radio, one session — and a holder that named no
    radio holds them all, because nothing can prove which one it opened."""


class SdrError(RuntimeError):
    """The radio could not be tuned, or the pipeline could not start."""


class SessionGone(RuntimeError):
    """This session was released, so it may not relaunch anything."""


def validate(frequency_hz: int, mode: str) -> str:
    """Bound the tuning request and return rtl_fm's demodulator name.

    Validated here as well as in the api because this process is the one that
    actually opens the device: a bound that lives only in the caller is a bound that
    a second caller does not have."""
    if not MIN_HZ <= frequency_hz <= MAX_HZ:
        raise SdrError(
            f"{frequency_hz} Hz is outside the tuner's range ({MIN_HZ}-{MAX_HZ} Hz)"
        )
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


def demod_args(mode: str, gain: str | None) -> list[str]:
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
    if MODES[mode] == "wbfm":
        args += ["-s", str(WBFM_SAMPLE_RATE), "-r", str(AUDIO_RATE)]
    else:
        args += ["-s", str(AUDIO_RATE)]
    if MODES[mode] == "am":
        args += ["-E", "dc"]
    if gain:
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
    bin_hz: int
    seconds: float

    @staticmethod
    def of(start_hz: int, stop_hz: int, bin_hz: int, seconds: float) -> "Sweep":
        start, stop = int(min(start_hz, stop_hz)), int(max(start_hz, stop_hz))
        if stop - start > MAX_SWEEP_SPAN_HZ:
            raise SdrError(
                f"a {(stop - start) / 1e6:.1f} MHz span is wider than this sweep allows "
                f"({MAX_SWEEP_SPAN_HZ / 1e6:.0f} MHz)"
            )
        if stop - start < 1:
            raise SdrError("a sweep needs a range, not a single frequency")
        return Sweep(
            start_hz=start,
            stop_hz=stop,
            bin_hz=max(MIN_SWEEP_BIN_HZ, min(int(bin_hz), MAX_SWEEP_BIN_HZ)),
            seconds=max(1.0, min(float(seconds), MAX_SWEEP_SECONDS)),
        )

    @property
    def centre_hz(self) -> int:
        """What the omnibox shows. A range has no one frequency, and showing the low
        edge would read as a tuner parked somewhere it is not."""
        return (self.start_hz + self.stop_hz) // 2


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """What a session looks like from outside — the shape the omnibox tuner reads."""

    session_id: str
    frequency_hz: int
    mode: str
    gain: str | None
    started_at: float
    peak: float
    listeners: int
    purpose: str = PURPOSE_LISTEN
    serial: str | None = None

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
            "peak": round(self.peak, 4),
            "listeners": self.listeners,
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
        if self.purpose == PURPOSE_SURVEY and sweep is None:
            raise SdrError("a survey session needs a sweep range")
        if self.purpose == PURPOSE_APRS and mode.lower() not in APRS_MODES:
            raise SdrError(
                f"APRS is 1200-baud AFSK on narrowband FM; {mode!r} cannot decode it "
                f"(want one of {sorted(APRS_MODES)})"
            )
        self.frequency_hz = frequency_hz
        self.mode = validate(frequency_hz, mode)
        self.peak = 0.0
        self._subs: set[queue.Queue[bytes | None]] = set()
        # Captioning subscribers. Segmenting only runs while at least one is attached,
        # so a session nobody is captioning does no extra work at all.
        self._segments: set[queue.Queue[tuple[float, bytes]]] = set()
        # Decoded APRS frames, for a purpose=aprs session. Empty on a listening one.
        self._packets: set[queue.Queue[packets.Packet | None]] = set()
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
        self._rtl: subprocess.Popen[bytes] | None = None
        self._enc: subprocess.Popen[bytes] | None = None
        self._threads: list[threading.Thread] = []
        self._start_pipeline()

    # ---- pipeline -------------------------------------------------------------

    def _rtl_cmd(self) -> list[str]:
        cmd = ["rtl_fm", "-f", str(self.frequency_hz), "-M", MODES[self.mode]]
        cmd += self._device_args()
        cmd += demod_args(self.mode, self.gain)
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

        rtl_power ONLY, and deliberately: `Dockerfile.sdr` states the rule outright —
        "No pip install at all: a radio sidecar that pulled a Python SDR stack would be
        carrying a second implementation of what librtlsdr already does" — and rtl_power
        is already in the image beside rtl_fm. The rewrites (`rtl_power_fftw`,
        `soapy_power`) are a source build and a pip install respectively, and neither
        has earned that against a measured shortfall yet.

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
                self.peak = level
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
        for proc in (self._rtl, self._enc):
            if proc is None:
                continue
            try:
                proc.kill()
                proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self._rtl = self._enc = None
        # The per-session direwolf config would otherwise accumulate in /tmp for the
        # life of the container, one file per lease taken.
        with contextlib.suppress(OSError):
            os.unlink(f"/tmp/direwolf-{self.id}.conf")  # noqa: S108 - written by this session

    # ---- public surface -------------------------------------------------------

    def tune(self, frequency_hz: int, mode: str | None = None) -> None:
        """Retune in place. Restarts the pipeline but keeps the session id.

        Refuses once the session has been released, and this is the load-bearing half.
        The route resolves the Session under the tuner's lock and calls this OUTSIDE it,
        so a `/listen/stop` landing in between used to make `_start_pipeline` spawn a
        fresh `rtl_fm` for a session no longer in `_sessions`: invisible to
        `blocking_key`, never reaped, and holding the dongle until the container
        restarts — after which the next caller for that serial is allowed through and
        two processes fight over one radio. That is the "garbled audio rather than an
        error" outcome the whole per-radio rule exists to prevent, so the guard belongs
        here rather than in the route, where the window is."""
        wanted = validate(frequency_hz, mode or self.mode)
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
            self.frequency_hz = frequency_hz
            self.mode = wanted
            self.peak = 0.0
            self._start_pipeline()
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

    @property
    def alive(self) -> bool:
        """Whether this session can still do its job.

        BOTH processes, not just the tuner. A logging session whose direwolf died —
        crashed, failed to bind, or wedged — was still reporting a healthy `aprs` lease
        while decoding nothing, and the owner's only clue would have been a log that
        stopped growing. `current()` reaps a dead session, so this is what turns a
        silent death into an idle radio the owner can see."""
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
            peak=self.peak,
            listeners=listeners,
            purpose=self.purpose,
            serial=self.serial,
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
    _SHOWN_FIRST = {PURPOSE_LISTEN: 0, PURPOSE_APRS: 1, PURPOSE_SURVEY: 2}

    @classmethod
    def _worth_showing(cls, live: list[Session]) -> Session | None:
        """The one to draw, out of a snapshot the caller already has."""
        if not live:
            return None
        return min(live, key=lambda s: (cls._SHOWN_FIRST.get(s.purpose, 3), s.serial or ""))

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
