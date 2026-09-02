"""The `sdr` sidecar: tune a USB software-defined radio, return audio.

The only container that touches the radio. It owns the device because the box has
exactly ONE tuner (docs/plans/SDR_RADIO_PLAN.md §4.2): listening, sweeping and
recording are mutually exclusive, so a single owner with a lock is what keeps two
callers from fighting over it. Serialising here rather than in the api means the
guarantee holds no matter who calls.

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
import json
import os
import queue
import shutil
import struct
import subprocess
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

from listen import PURPOSE_LABEL, PURPOSE_LISTEN, PURPOSES
from listen import SdrBusy as ListenBusy
from listen import SdrError as ListenError
from listen import AUDIO_CONTENT_TYPE, AUDIO_RATE, Tuner

# The R820T2 tuner's real range. Anything outside it cannot be tuned, so it is
# rejected here rather than handed to rtl_fm to fail on. HF below 24 MHz needs
# direct sampling and is deliberately out of scope for now (plan §9).
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
_LOCK = threading.Lock()

# The one live listening session this box can have. Separate from _LOCK, which
# guards the one-shot `capture` path: a capture and a listen both want the tuner,
# so each refuses while the other holds it.
TUNER = Tuner()


class SdrBusy(RuntimeError):
    """Another capture holds the tuner. One radio, one caller."""


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

    Holds the single-tuner lock for the whole capture and refuses rather than queues:
    a caller waiting an unknown time on a radio someone else is using is worse than a
    caller told plainly that it is busy."""
    if not MIN_HZ <= freq_hz <= MAX_HZ:
        raise SdrError(f"{freq_hz} Hz is outside the tuner's range ({MIN_HZ}-{MAX_HZ} Hz)")
    key = mode.lower()
    if key not in MODES:
        raise SdrError(f"unknown mode {mode!r} (want one of {sorted(MODES)})")
    seconds = max(0.5, min(float(seconds), MAX_SECONDS))

    live = TUNER.current()
    if live is not None:
        held = PURPOSE_LABEL.get(live.purpose, "in use")
        raise SdrBusy(f"the radio is busy — it is {held}")
    if not _LOCK.acquire(blocking=False):
        raise SdrBusy("the radio is busy with another capture")
    try:
        rate = NARROW_RATE
        cmd = ["rtl_fm", "-f", str(freq_hz), "-M", MODES[key]]
        if MODES[key] == "wbfm":
            # wbfm forces its own capture rate; -r resamples the output for us.
            cmd += ["-s", str(WBFM_SAMPLE_RATE), "-r", str(rate)]
        else:
            cmd += ["-s", str(rate)]
        if serial:
            cmd += ["-d", f"serial={serial}"]
        if gain:
            cmd += ["-g", gain]
        cmd += ["-"]

        # rtl_fm streams until killed, so the timeout IS the recording length. A
        # generous kill margin covers device open + tuner settle on a cold radio.
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=seconds, check=False)
            pcm, err = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as expired:
            pcm = expired.stdout or b""
            err = expired.stderr or b""

        text = err.decode("utf-8", "replace")
        if not pcm:
            raise SdrError(f"rtl_fm produced no audio: {text.strip()[-400:] or 'no output'}")

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
        _LOCK.release()


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
        if route != "/healthz":
            self._json(404, {"detail": "not found"})
            return
        # Report whether the tools are even present: a sidecar that starts but has no
        # rtl_fm is a failure the api should see at /healthz, not on first capture.
        session = TUNER.current()
        self._json(
            200,
            {
                "status": "ok",
                "rtl_fm": shutil.which("rtl_fm") is not None,
                "ffmpeg": shutil.which("ffmpeg") is not None,
                "busy": _LOCK.locked(),
                # What jobs this sidecar knows how to hold the tuner for. Advertised
                # because an OLDER sidecar ignores an unknown `purpose` and returns 200
                # with a plain listening session — so "turn APRS logging on" against a
                # box that has not been updated would succeed, log nothing, and report
                # success. A caller can check here instead of trusting a 200.
                "purposes": list(PURPOSES),
                "listening": session.info().as_dict() if session is not None else None,
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
        session = TUNER.current()
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
        session = TUNER.current()
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

    def _listen(self, body: dict[str, Any]) -> None:
        try:
            info = TUNER.start(
                frequency_hz=int(body.get("frequency_hz", 0)),
                mode=str(body.get("mode", "fm")),
                gain=body.get("gain"),
                # Absent means listening: every existing caller predates purposes and
                # means exactly that, so the default keeps them byte-identical.
                purpose=str(body.get("purpose") or PURPOSE_LISTEN),
            )
        except ListenBusy as busy:
            self._json(409, {"detail": str(busy)})
            return
        except (ListenError, ValueError) as bad:
            self._json(400, {"detail": str(bad)})
            return
        self._json(200, info.as_dict())

    def _tune(self, body: dict[str, Any]) -> None:
        session = TUNER.current()
        if session is not None and session.purpose != PURPOSE_LISTEN:
            # Retuning a logging session would move the packet channel to wherever the
            # tuner sheet asked for, leave the lease claiming to be logging APRS, and
            # refuse the next caller with a reason that had become false. Releasing is
            # the only honest way out of one job into another.
            self._json(
                409,
                {"detail": f"the radio is {PURPOSE_LABEL.get(session.purpose, 'in use')}"},
            )
            return
        if session is None:
            self._json(409, {"detail": "nothing is listening"})
            return
        wanted = body.get("session_id")
        if wanted is not None and wanted != session.id:
            # A stale client retuning a session that has since been replaced would
            # otherwise silently move someone else's radio.
            self._json(409, {"detail": "that session is no longer the live one"})
            return
        try:
            session.tune(int(body.get("frequency_hz", 0)), body.get("mode"))
        except (ListenError, ValueError) as bad:
            self._json(400, {"detail": str(bad)})
            return
        self._json(200, session.info().as_dict())

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
                stopped = TUNER.stop(body.get("session_id"))
                self._json(200, {"stopped": stopped})
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
                serial=body.get("serial"),
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
