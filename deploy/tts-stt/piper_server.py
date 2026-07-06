#!/usr/bin/env python3
"""Warm-model piper TTS server for the JBrain2 `tts-stt` speech service.

The box's read-aloud voice: renders answer text to WAV with piper and returns the
audio. Unlike the old per-clip `piper` subprocess (which COLD-LOADED the ~60-78 MB
model every render — the dominant latency and the source of gappy playback), this
holds each voice's model resident via `piper.voice.PiperVoice` and reuses it, so a
clip renders in ~0.1 s instead of ~1.5 s. It shares the `tts-stt` container with the
whisper.cpp STT server (llama-swap, a separate process on :8080); this is the TTS
half, on :8801.

Reached over the internal docker network only (no LAN port): the authenticated api
proxies it for the PWA read-aloud + Settings sample (`/api/brain/tts`,
`/api/brain/voices`), and the `wall` display forwards its own read-aloud here. It
touches NO database and NO user data — only the answer TEXT the owner asked to be
read, rendered to audio and returned; nothing is stored.

A single-speaker `.onnx` (+ its `.onnx.json`) is one selectable voice named by its
file stem; a MULTI-speaker model (e.g. libritts_r) contributes one voice per CURATED
speaker, id "<stem>#<speaker>". Voices are found across the mounted extras dir then
the baked defaults dir, so the service needs no configuration — Joe/Amy (+ the
multi-speaker libritts_r) are baked into the image.

Stdlib + piper only — no web framework.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import threading
import time
import urllib.parse
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from piper import PiperVoice, SynthesisConfig

PIPER_VOICES_DIR = Path(os.environ.get("BRAIN_PIPER_VOICES_DIR", "/opt/piper-voices"))
PIPER_BAKED_VOICES_DIR = Path(os.environ.get("BRAIN_PIPER_BAKED_VOICES_DIR", "/opt/piper-voices"))
# A short lead of silence so a cold audio-sink resume clips the silence, not the first
# word (the page's WebAudio keep-alive is the real fix; this is a backstop). Requested
# only on the FIRST clip of a turn — continuation clips send ?lead=0 for gapless playback.
PIPER_LEAD_MS = int(os.environ.get("BRAIN_PIPER_LEAD_MS", "400"))
# Longest single render the page requests — it splits a reply into sentence-sized clips,
# so this bounds one chunk, not the whole answer.
TTS_CHUNK_CAP = 1000
# Pre-warm this voice at startup so the very first read-aloud clip doesn't pay the
# one-time model load (~8 s on a big multi-speaker model). "" skips pre-warming.
PREWARM_VOICE = os.environ.get("BRAIN_PIPER_PREWARM", "en_US-amy-medium")

# A multi-speaker piper model carries hundreds of speakers; we surface only a curated
# few as named voices (id "<stem>#<speaker>"), keyed by model file stem -> the speaker
# names to expose (as they appear in the model's `.onnx.json` speaker_id_map). libritts_r
# speaker 3922 (piper index 0) is a second, female agent voice. Add names to expose more.
CURATED_SPEAKERS: dict[str, tuple[str, ...]] = {
    "en_US-libritts_r-medium": ("3922",),
}

# Verbose per-clip tracing, pushed on by the app ({"kind": "tts_debug", "on": bool} to
# /event) while a debug-console token is live — logs each render's voice-as-received,
# resolved speaker, bytes and elapsed ms. Failures ALWAYS log; this adds the success trace.
_tts_debug = [False]

# Warm model cache: model path -> loaded PiperVoice, plus a lock serialising synthesis
# (onnxruntime is not safe to Run concurrently on one session — renders are fast, so a
# single global lock is simplest and cheap).
_voice_cache: dict[str, PiperVoice] = {}
_synth_lock = threading.Lock()


def _voice_models() -> dict[str, Path]:
    """Map each model file stem to its `.onnx` path, mounted extras dir then baked
    defaults. Earlier dirs win on a name clash. {} if none."""
    models: dict[str, Path] = {}
    for d in (PIPER_VOICES_DIR, PIPER_BAKED_VOICES_DIR):
        try:
            for p in sorted(d.glob("*.onnx")):
                models.setdefault(p.stem, p)
        except OSError:
            continue
    return models


def _speaker_map(model: Path) -> dict[str, int]:
    """`{speaker_name: index}` from a model's `.onnx.json` `speaker_id_map`, or {} for a
    single-speaker model. Only name->int entries survive, so a junk map can't yield a bad
    speaker id."""
    try:
        meta = json.loads(Path(str(model) + ".json").read_text())
    except (OSError, ValueError):
        return {}
    sid = meta.get("speaker_id_map")
    if not isinstance(sid, dict):
        return {}
    return {str(k): v for k, v in sid.items() if isinstance(v, int) and not isinstance(v, bool)}


def _voices() -> list[tuple[str, Path, int | None]]:
    """The selectable voices as `(id, model_path, speaker_index)`, stems sorted. Single-
    speaker -> one entry (id = stem, speaker None). Multi-speaker -> one entry per CURATED
    speaker present (id "<stem>#<name>"), else its default speaker (id = stem, index 0)."""
    out: list[tuple[str, Path, int | None]] = []
    for stem, model in sorted(_voice_models().items()):
        smap = _speaker_map(model)
        if not smap:
            out.append((stem, model, None))
            continue
        names = [n for n in CURATED_SPEAKERS.get(stem, ()) if n in smap]
        if names:
            out.extend((f"{stem}#{n}", model, smap[n]) for n in names)
        else:
            out.append((stem, model, 0))  # multi-speaker but uncurated -> default speaker
    return out


def piper_voices() -> list[str]:
    """Installed selectable voice ids (incl. curated multi-speaker entries), or []."""
    return [vid for vid, _, _ in _voices()]


def _resolve_voice(voice_id: str) -> tuple[Path, int | None] | None:
    """Map a requested voice id to `(model_path, speaker_index)`, validated against the
    installed set (no path traversal). An unknown/blank id falls back to the first voice;
    None only when nothing is installed."""
    voices = _voices()
    if not voices:
        return None
    for vid, model, spk in voices:
        if vid == voice_id:
            return model, spk
    _, model, spk = voices[0]
    return model, spk


def _load_voice(model: Path) -> PiperVoice:
    """Return the resident PiperVoice for `model`, loading (and caching) it on first use.
    The load is the expensive step (~1-8 s); every render after reuses it."""
    key = str(model)
    voice = _voice_cache.get(key)
    if voice is None:
        voice = PiperVoice.load(key, config_path=f"{key}.json")
        _voice_cache[key] = voice
    return voice


def _pad_lead(wav_bytes: bytes, lead_ms: int) -> bytes:
    """Prepend `lead_ms` of silence to a WAV (stdlib only), so a sink resume clips silence."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            params = w.getparams()
            frames = w.readframes(w.getnframes())
        frame = params.sampwidth * params.nchannels
        pad = b"\x00" * (int(params.framerate * lead_ms / 1000) * frame)
        out = io.BytesIO()
        with wave.open(out, "wb") as o:
            o.setparams(params)
            o.writeframes(pad + frames)
        return out.getvalue()
    except (wave.Error, OSError, EOFError, ValueError):
        return wav_bytes


def _silence_wav(ms: int) -> bytes:
    """A short mono silent WAV — the page plays it once when read-aloud activates to PRIME
    the <audio> -> sink path so the first real clip isn't clipped by the sink's cold start."""
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x00" * int(22050 * max(ms, 0) / 1000))
    return out.getvalue()


def tts_wav(text: str, voice: str, lead_ms: int | None = None) -> bytes | None:
    """Render `text` to a WAV with the warm piper model for `voice` (a voice id validated
    against the installed set — no path traversal). For a multi-speaker voice
    ("<stem>#<speaker>") the resolved index is passed as the synthesis speaker_id. None
    when TTS isn't available or rendering fails; every None path is logged (a silent None
    degrades the reply to the device's native voice, which looks like the wrong voice)."""
    resolved = _resolve_voice(voice)
    if resolved is None:
        print(f"[tts] render failed for {voice!r}: no voices installed", file=sys.stderr)
        return None
    model, speaker = resolved
    if not model.exists():
        print(f"[tts] render failed for {voice!r}: model file {model} missing", file=sys.stderr)
        return None
    if _tts_debug[0]:
        print(f"[tts] rendering {voice!r} -> {model.stem} speaker={speaker} ({len(text)} chars)",
              file=sys.stderr)
    started = time.monotonic()
    try:
        voice_model = _load_voice(model)
        buf = io.BytesIO()
        wf = wave.open(buf, "wb")  # noqa: SIM115 — manual close in finally (below)
        try:
            with _synth_lock:
                voice_model.synthesize_wav(text, wf, syn_config=SynthesisConfig(speaker_id=speaker))
        finally:
            # If synth raised before setting the WAV format, close() would raise
            # "# channels not specified" and MASK the real piper error — suppress it so the
            # true cause reaches the log below.
            with contextlib.suppress(Exception):
                wf.close()
        data = buf.getvalue()
    except Exception as exc:  # noqa: BLE001 — any synth failure must surface, not crash the server
        print(f"[tts] render failed for {voice!r} (speaker {speaker}): "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return None
    if _tts_debug[0]:
        ms = int((time.monotonic() - started) * 1000)
        print(f"[tts] rendered {voice!r} (speaker {speaker}): {len(data)} bytes in {ms} ms",
              file=sys.stderr)
    lead = PIPER_LEAD_MS if lead_ms is None else lead_ms
    return _pad_lead(data, lead) if lead > 0 else data


def _prewarm() -> None:
    """Load the default answer voice at startup so the first clip doesn't pay the one-time
    model load. Best-effort — a missing/failing voice just leaves it lazy."""
    if not PREWARM_VOICE:
        return
    resolved = _resolve_voice(PREWARM_VOICE)
    if resolved is None or not resolved[0].exists():
        return
    try:
        _load_voice(resolved[0])
        print(f"[tts] pre-warmed {resolved[0].stem}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"[tts] pre-warm of {PREWARM_VOICE!r} failed: {exc}", file=sys.stderr)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send(200, b"ok", "text/plain")
        elif path == "/tts/voices":
            self._send(200, json.dumps({"voices": piper_voices()}).encode(), "application/json")
        elif path == "/tts/silence":
            self._send(200, _silence_wav(600), "audio/wav")
        elif path == "/tts":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            text = qs.get("text", [""])[0][:TTS_CHUNK_CAP]
            if not text.strip():
                self._send(400, b"no text", "text/plain")
                return
            lead_ms = None
            try:
                raw = qs.get("lead", [None])[0]
                if raw is not None:
                    lead_ms = max(0, min(2000, int(raw)))
            except (TypeError, ValueError):
                lead_ms = None
            wav = tts_wav(text, qs.get("voice", [""])[0], lead_ms)
            if wav is None:
                self._send(503, b"tts unavailable", "text/plain")
            else:
                self._send(200, wav, "audio/wav")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        # The app pushes {"kind": "tts_debug", "on": bool} while a debug-console token is
        # live, switching on the verbose per-clip trace. Latched like the wall's flags.
        if self.path.split("?", 1)[0] != "/event":
            self._send(404, b"not found", "text/plain")
            return
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 16384)
            ev = json.loads(self.rfile.read(n) if n > 0 else b"{}")
        except (ValueError, OSError):
            self._send(400, b"bad request", "text/plain")
            return
        kind = ev.get("kind") if isinstance(ev, dict) else None
        if kind == "tts_debug":
            _tts_debug[0] = bool(ev.get("on"))
            self._send(204, b"", "text/plain")
        else:
            self._send(400, b"unknown kind", "text/plain")

    def log_message(self, *args) -> None:  # quiet; runs as a background service
        pass


def main() -> None:
    host = os.environ.get("BRAIN_TTS_HOST", "0.0.0.0")
    port = int(os.environ.get("BRAIN_TTS_PORT", "8801"))
    _prewarm()
    server = ThreadingHTTPServer((host, port), Handler)
    installed = ", ".join(piper_voices()) or "none"
    print(f"piper tts server on http://{host}:{port}/  (voices: {installed})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
