"""The live listening session: one tuned radio, streamed as Opus while it runs.

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
we measure it, then write it on to ffmpeg, which encodes Opus in a WebM container
that an ordinary `<audio>` element plays from a chunked response.

**Retuning restarts the pipeline.** `rtl_fm` cannot be retuned in place. A tune
therefore tears down and relaunches at the new frequency, but keeps the SESSION id,
so the UI sees a continuous session with a brief audio gap rather than a session
that vanished and came back — which would flicker the icon and, worse, read as the
lease having been dropped.
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

AUDIO_RATE = 16_000  # whisper's native rate, and rtl_fm's for narrowband
WBFM_SAMPLE_RATE = 171_000  # rtl_fm's documented wide-FM capture rate
_CHUNK = 4096

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

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "frequency_hz": self.frequency_hz,
            "mode": self.mode,
            "gain": self.gain,
            "started_at": self.started_at,
            "elapsed_s": round(time.time() - self.started_at, 1),
            "peak": round(self.peak, 4),
            "listeners": self.listeners,
        }


class Session:
    """One tuned radio, running until stopped."""

    def __init__(self, frequency_hz: int, mode: str, gain: str | None) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.started_at = time.time()
        self.gain = gain
        self.frequency_hz = frequency_hz
        self.mode = validate(frequency_hz, mode)
        self.peak = 0.0
        self._subs: set[queue.Queue[bytes | None]] = set()
        self._lock = threading.Lock()
        self._stopping = False
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
        # Opus in WebM: what a plain <audio src> plays from a chunked response.
        # -flush_packets keeps latency down; without it the muxer buffers and the
        # first sound arrives seconds late, which reads as a broken radio.
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "s16le", "-ar", str(AUDIO_RATE), "-ac", "1", "-i", "-",
            "-c:a", "libopus", "-b:a", "24k", "-application", "voip",
            "-f", "webm", "-flush_packets", "1", "-",
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
        ]
        for thread in self._threads:
            thread.start()

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
                self.peak = _peak(chunk)
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
        self._stopping = True
        self._kill()
        for thread in self._threads:
            thread.join(timeout=2)
        self._stopping = False
        self.frequency_hz = frequency_hz
        self.mode = wanted
        self.peak = 0.0
        self._start_pipeline()

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
        )


class Tuner:
    """Holds the one session the box can have, and refuses a second."""

    def __init__(self) -> None:
        self._session: Session | None = None
        self._lock = threading.Lock()

    def start(self, frequency_hz: int, mode: str, gain: str | None) -> SessionInfo:
        with self._lock:
            if self._session is not None and self._session.alive:
                raise SdrBusy("the radio is already listening")
            if self._session is not None:
                self._session.stop()  # a dead session must not block a new one
            self._session = Session(frequency_hz, mode, gain)
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
