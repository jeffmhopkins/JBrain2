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

Zero new Python dependencies: the HTTP surface is `http.server` from the stdlib and
the radio work is `rtl_fm` (from the `rtl-sdr` package) over a pipe. What comes back
is a 16 kHz mono WAV, which is exactly what whisper wants — no resampling stage, a
convenience of rtl_fm's native output rather than a coincidence.
"""

from __future__ import annotations

import io
import contextlib
import json
import os
import queue
import shutil
import struct
import subprocess
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

import listen
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

# The R820T2 tuner's real range. Anything outside it cannot be tuned, so it is
# rejected here rather than handed to rtl_fm to fail on. HF below 24 MHz needs
# direct sampling and is deliberately out of scope for now (plan §9).
# How long after rtl_power's own exit timer to keep waiting, for the retune settle it
# does before the first row. Named so a test can shrink it.
SWEEP_SETTLE_S = 30

MIN_HZ = 24_000_000
MAX_HZ = 1_766_000_000

# rtl_fm's demodulators. Narrow FM for voice comms, wide FM for broadcast, AM for
# air band. The values are passed to `-M`, so this doubles as the allowlist.
MODES = {
    "fm": "fm",
    "nfm": "fm",
    "wbfm": "wbfm",
    "am": "am",
    "usb": "usb",
    "lsb": "lsb",
}

# 16 kHz mono is whisper's native input AND rtl_fm's, for narrowband. Wide FM needs a
# higher demodulation rate to sound right, so it is captured at 32 kHz and told to
# resample down — rtl_fm does that itself with `-r`.
NARROW_RATE = 16_000
WBFM_SAMPLE_RATE = 171_000  # rtl_fm's documented wbfm capture rate

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


def _range_of(body: dict[str, Any]) -> listen.Sweep:
    """The span a sweeping request is asking for, bounds and all.

    Shared by the one-shot `/sweep` and by starting or moving a live spectrum, so the
    three cannot drift apart on what a legal range is — the tuner's limits are a fact
    about the radio, not about which route asked.

    `seconds` is how long a SURVEY runs and means nothing to a live spectrum, which
    runs until it is released. It is parsed either way rather than made conditional:
    `Sweep` is one type, and a field a caller may ignore is cheaper than two types that
    agree about everything else."""
    sweep = listen.Sweep.of(
        start_hz=int(body.get("start_hz", 0)),
        stop_hz=int(body.get("stop_hz", 0)),
        bin_hz=int(body.get("bin_hz", 25_000)),
        seconds=float(body.get("seconds", 60)),
    )
    if not (MIN_HZ <= sweep.start_hz and sweep.stop_hz <= MAX_HZ):
        raise ListenError(
            f"{sweep.start_hz}-{sweep.stop_hz} Hz is outside the tuner's range "
            f"({MIN_HZ}-{MAX_HZ} Hz)"
        )
    return sweep


def _peak(pcm: bytes) -> float:
    """Loudest sample as a 0..1 fraction of full scale.

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
            "peak": _peak(pcm),
            "device_log": text.strip()[-2000:],
            "wav": _wav(pcm, rate),
        }
    finally:
        TUNER.unreserve(serial)


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, body: dict[str, Any]) -> None:
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

    def _stream_spectrum(self) -> None:
        """Hand one viewer a stream of waterfall rows, newline-framed JSON.

        Same shape as `/listen/packets`, and for the same two reasons: one JSON object
        per line so a reader needs no framing of its own, and keep-alives so a stream
        that is between frames does not look like a dead socket.

        Each row carries its own range (`listen.Frame`), so the api relays lines without
        understanding them and a retune needs no message of its own."""
        session = TUNER.for_purpose(PURPOSE_SPECTRUM)
        if session is None:
            held = TUNER.sessions()
            if held:
                # Name the holder rather than saying "idle", which is false and sends
                # the owner looking for a radio that is plainly in use.
                doing = PURPOSE_LABEL.get(held[0].purpose, "in use")
                detail = f"the radio is {doing}, not watching the spectrum"
                self._json(409, {"detail": detail})
                return
            self._json(409, {"detail": "nothing is watching the spectrum"})
            return
        sub = session.subscribe_frames()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
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
        """Run one band sweep and return the CSV rtl_power wrote.

        The sidecar's whole job here is the RADIO: hold the lease, run the sweep, hand
        back the numbers. It does not reduce them and it does not draw them, because
        `Dockerfile.sdr` forbids the pip install a plotting stack would need — and the
        api already carries Pillow for exactly this kind of work. Sending the raw CSV
        also means a better reduction can be run over an old sweep later, the same
        reasoning that keeps `raw` on every APRS row.

        Synchronous: the request is held for the length of the sweep. That is the
        caller's problem to solve (the debug route runs it as a background job), not a
        reason for the sidecar to grow a job table of its own."""
        body = self._body()
        if body is None:
            return
        try:
            sweep = _range_of(body)
        except (TypeError, ValueError) as bad:
            self._json(400, {"detail": f"a sweep needs numbers: {bad}"})
            return
        except ListenError as refused:
            self._json(400, {"detail": str(refused)})
            return

        try:
            info = TUNER.start(
                sweep.centre_hz,
                "fm",
                body.get("gain"),
                purpose=listen.PURPOSE_SURVEY,
                sweep=sweep,
                serial=listen.validate_serial(body.get("serial")),
            )
        except ListenBusy as busy:
            self._json(409, {"detail": str(busy)})
            return
        except ListenError as bad:
            self._json(400, {"detail": str(bad)})
            return

        # The path is derived from the session id rather than read off the Session,
        # because a sweep can finish before this line runs — `Tuner.current()` reaps a
        # dead session, so holding the object is a race the fast case loses.
        csv_path = f"/tmp/sweep-{info.session_id}.csv"  # noqa: S108 - container-local
        # ...and the Session itself is held anyway, for the OPPOSITE reason: once it is
        # reaped its stderr is gone with it, and that is the only place a sweep that
        # measured nothing says why. A reference kept here outlives the registry.
        ran = TUNER.find(info.session_id)
        # rtl_power's own exit timer ends it; the wait is bounded by that plus a margin
        # for the retune settle it does before the first row. The tuner going idle IS
        # the completion signal, since a survey frees the radio when it ends.
        deadline = time.time() + sweep.seconds + SWEEP_SETTLE_S
        stopped_early = True
        while time.time() < deadline:
            held = TUNER.find(info.session_id)
            if held is None:
                stopped_early = False
                break
            time.sleep(0.25)
        TUNER.stop(info.session_id)

        try:
            with open(csv_path, encoding="utf-8", errors="replace") as handle:
                csv_text = handle.read()
        except OSError:
            csv_text = ""
        with contextlib.suppress(OSError):
            os.unlink(csv_path)

        if not csv_text.strip():
            # A sweep that measured NOTHING is a failure, not a quiet band, and it used
            # to answer `complete: true` with an empty CSV — a success over a
            # measurement that never happened. MEASURED on the box 2026-09-04: rtl_power
            # exited on `No matching devices found` and the caller was told the sweep
            # finished. The tool's own last words go back with the refusal, because they
            # name WHICH radio it could not find and the owner cannot read the container
            # log (CLAUDE.md #10).
            # The refusal line first: the rest of the tail is hop plans and buffer
            # sizes, and leading with those buries the sentence that names what to fix.
            said = (ran.refusal or ran.tail) if ran else ""
            self._json(
                502,
                {"detail": f"the sweep measured nothing: {said or 'the tool wrote no rows'}"},
            )
            return

        self._json(
            200,
            {
                "start_hz": sweep.start_hz,
                "stop_hz": sweep.stop_hz,
                "bin_hz": sweep.bin_hz,
                "seconds": sweep.seconds,
                "gain": body.get("gain"),
                # A sweep that hit the deadline still returns its rows. A partial sweep
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
        try:
            usbdev.reset(node)
        except ValueError as bad:
            self._json(400, {"detail": str(bad)})
            return
        except OSError as failed:
            # ENODEV is the ordinary one: the node moved between the scan and now, which
            # a reset itself causes. Said plainly, because "try again" really is the fix.
            self._json(
                502,
                {"detail": f"the radio would not reset ({failed.strerror or failed}). "},
            )
            return
        finally:
            TUNER.unreserve(named)
        # The node number changes as a result — a reset device comes back at the next
        # free address — so the caller is told to look again rather than reuse it.
        self._json(200, {"reset": True, "serial": named, "was": node})

    def _listen(self, body: dict[str, Any]) -> None:
        # Absent means listening: every existing caller predates purposes and means
        # exactly that, so the default keeps them byte-identical.
        purpose = str(body.get("purpose") or PURPOSE_LISTEN)
        sweep = None
        if purpose in SWEEPING:
            try:
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
            )
        except ListenBusy as busy:
            self._json(409, {"detail": str(busy)})
            return
        except (ListenError, ValueError) as bad:
            self._json(400, {"detail": str(bad)})
            return
        self._json(200, info.as_dict())

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
            session.tune(int(body.get("frequency_hz", 0)), body.get("mode"))
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
        if route in ("/listen/start", "/listen/tune", "/listen/stop"):
            body = self._body()
            if body is None:
                return
            if route == "/listen/start":
                self._listen(body)
            elif route == "/listen/tune":
                self._tune(body)
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

    def log_message(self, fmt: str, *args: Any) -> None:
        # Default logging writes to stderr per request; the container log is the
        # audit trail we want, so keep it but without the noisy address prefix.
        print(f"[sdr] {fmt % args}", flush=True)  # noqa: T201


def main() -> None:
    port = int(os.environ.get("SDR_PORT", "8000"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()
