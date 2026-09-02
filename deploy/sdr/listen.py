"""The live listening session: one tuned radio, streamed as MP3 while it runs.

This is the lease made real. The box has exactly one tuner, so there is at most one
Session at a time and everything else is told 409 rather than queued — an unknown
wait on a radio someone else is using is worse than a plain no
(docs/plans/SDR_RADIO_PLAN.md §4.2). The session's EXISTENCE is what the omnibox
icon reads, so "is a session running" and "does the owner see a radio icon" are the
same fact rather than two that can disagree.

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

import queue
import shutil
import struct
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

MIN_HZ = 24_000_000
MAX_HZ = 1_766_000_000
MODES = {"fm": "fm", "nfm": "fm", "wbfm": "wbfm", "am": "am", "usb": "usb", "lsb": "lsb"}

# What a session is HOLDING the tuner for. One radio, so the lease is the only arbiter,
# and which job holds it decides what the loser is told: "release it to listen" and
# "release it to log" are opposite advice (docs/plans/APRS_CONTROL_PLAN.md P0).
# `listen` produces audio; `aprs` decodes packets and produces none.
PURPOSE_LISTEN = "listen"
PURPOSE_APRS = "aprs"
# Every purpose maps to the phrase a refusal uses. `.get` rather than `[]` because this
# is read while holding the tuner lock on the contention path: a purpose added without a
# label would otherwise raise KeyError there and turn a 409 into a 500 with a traceback.
PURPOSE_LABEL = {PURPOSE_LISTEN: "listening", PURPOSE_APRS: "logging APRS"}
PURPOSES = tuple(PURPOSE_LABEL)

AUDIO_RATE = 16_000  # whisper's native rate, and rtl_fm's for narrowband
AUDIO_BITRATE = "64k"  # MP3 at 16 kHz mono; the demodulated audio is the ceiling, not this
WBFM_SAMPLE_RATE = 171_000  # rtl_fm's documented wide-FM capture rate
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

# A subscriber that stops reading (a closed tab, a stalled phone) must not wedge the
# pump or grow without bound. Its queue is small and we DROP for that subscriber
# rather than block everyone — live audio is worthless late, so dropping is the
# correct backpressure here.
_SUB_QUEUE_CHUNKS = 64


class SdrBusy(RuntimeError):
    """The tuner is already held. One radio, one session."""


class SdrError(RuntimeError):
    """The radio could not be tuned, or the pipeline could not start."""


def validate(frequency_hz: int, mode: str) -> str:
    """Bound the tuning request and return rtl_fm's demodulator name.

    Validated here as well as in the api because this process is the one that
    actually opens the device: a bound that lives only in the caller is a bound that
    a second caller does not have."""
    if not MIN_HZ <= frequency_hz <= MAX_HZ:
        raise SdrError(f"{frequency_hz} Hz is outside the tuner's range ({MIN_HZ}-{MAX_HZ} Hz)")
    key = mode.lower()
    if key not in MODES:
        raise SdrError(f"unknown mode {mode!r} (want one of {sorted(MODES)})")
    return key


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

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "frequency_hz": self.frequency_hz,
            "mode": self.mode,
            "gain": self.gain,
            "purpose": self.purpose,
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
    ) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.started_at = time.time()
        self.gain = gain
        self.purpose = validate_purpose(purpose)
        self.frequency_hz = frequency_hz
        self.mode = validate(frequency_hz, mode)
        self.peak = 0.0
        self._subs: set[queue.Queue[bytes | None]] = set()
        # Captioning subscribers. Segmenting only runs while at least one is attached,
        # so a session nobody is captioning does no extra work at all.
        self._segments: set[queue.Queue[tuple[float, bytes]]] = set()
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
        self._rtl: subprocess.Popen[bytes] | None = None
        self._enc: subprocess.Popen[bytes] | None = None
        self._threads: list[threading.Thread] = []
        self._start_pipeline()

    # ---- pipeline -------------------------------------------------------------

    def _rtl_cmd(self) -> list[str]:
        cmd = ["rtl_fm", "-f", str(self.frequency_hz), "-M", MODES[self.mode]]
        if MODES[self.mode] == "wbfm":
            cmd += ["-s", str(WBFM_SAMPLE_RATE), "-r", str(AUDIO_RATE)]
        else:
            cmd += ["-s", str(AUDIO_RATE)]
        if self.gain:
            cmd += ["-g", self.gain]
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

    # ---- public surface -------------------------------------------------------

    def tune(self, frequency_hz: int, mode: str | None = None) -> None:
        """Retune in place. Restarts the pipeline but keeps the session id."""
        wanted = validate(frequency_hz, mode or self.mode)
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
        self._stopping = True
        self._kill()
        with self._lock:
            subs = list(self._subs)
            self._subs.clear()
        for sub in subs:
            try:
                sub.put_nowait(None)
            except queue.Full:
                pass

    @property
    def alive(self) -> bool:
        return self._rtl is not None and self._rtl.poll() is None

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
        )


class Tuner:
    """Holds the one session the box can have, and refuses a second."""

    def __init__(self) -> None:
        self._session: Session | None = None
        self._lock = threading.Lock()

    def start(
        self,
        frequency_hz: int,
        mode: str,
        gain: str | None,
        purpose: str = PURPOSE_LISTEN,
    ) -> SessionInfo:
        validate_purpose(purpose)
        with self._lock:
            if self._session is not None and self._session.alive:
                held = PURPOSE_LABEL.get(self._session.purpose, "in use")
                raise SdrBusy(f"the radio is already {held}")
            if self._session is not None:
                self._session.stop()  # a dead session must not block a new one
            self._session = Session(frequency_hz, mode, gain, purpose)
            return self._session.info()

    def current(self) -> Session | None:
        with self._lock:
            if self._session is not None and not self._session.alive:
                # rtl_fm died (device unplugged, driver reclaimed it). Report idle
                # rather than a session that cannot produce audio.
                self._session.stop()
                self._session = None
            return self._session

    def stop(self, session_id: str | None = None) -> bool:
        with self._lock:
            session = self._session
            if session is None:
                return False
            if session_id is not None and session_id != session.id:
                return False
            session.stop()
            self._session = None
            return True
