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
import re
import sys
import threading
import time
import urllib.parse
import wave
from array import array
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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

# --- Kokoro-82M: a second, more natural TTS engine baked beside piper (Apache-2.0) ----------
# Unlike piper (one .onnx per voice), Kokoro is ONE onnx model + a voice-styles bin that
# together serve many voices; both live in their OWN dir so the piper `*.onnx` glob never tries
# to load the Kokoro model as a PiperVoice. We surface a set as ids "kokoro-<voice>", selectable
# ONLY when the weights are baked on disk — a box without them lists no Kokoro voices rather than
# dead entries. Keep KOKORO_MODEL/KOKORO_VOICES_FILE in step with the bake block in
# Dockerfile.tts-stt (the test guards this).
KOKORO_DIR = Path(os.environ.get("BRAIN_KOKORO_DIR", "/opt/kokoro"))
KOKORO_MODEL = "kokoro-v1.0.onnx"
KOKORO_VOICES_FILE = "voices-v1.0.bin"
KOKORO_ID_PREFIX = "kokoro-"
# Audiobook pacing (env-tunable; both default to NO-OP so chat answers are unchanged — the owner
# dials them in by ear after a listen). SPEED < 1.0 reads slower/warmer; TRAIL_MS appends silence
# after each Kokoro clip for a beat between sentences. Per-request `speed`/`trail` on /tts override
# these (for the future story-vs-answer modes). Kokoro only; clamped at use. Parsed defensively so a
# typo'd env can't crash the always-on service (a crash here would take piper down too).
try:
    KOKORO_SPEED = float(os.environ.get("BRAIN_KOKORO_SPEED", "1.0"))
except ValueError:
    KOKORO_SPEED = 1.0
try:
    KOKORO_TRAIL_MS = int(os.environ.get("BRAIN_KOKORO_TRAIL_MS", "0"))
except ValueError:
    KOKORO_TRAIL_MS = 0
# The exposed Kokoro voices: the ENGLISH v1.0 roster only (American af_/am_, British bf_/bm_) —
# read-aloud renders with lang="en-us", so the model's French/Japanese/etc. voices would
# mispronounce English and are deliberately omitted. Names are the model's own voice ids and are
# stable for the pinned v1.0 weights; af_heart (Kokoro's default/highest-quality voice) leads so
# it's the pick when the owner first switches to Kokoro. The Settings picker groups these under a
# single "Kokoro" entry that reveals a second dropdown of the full list.
CURATED_KOKORO_VOICES: tuple[str, ...] = (
    # American English — female
    "af_heart", "af_bella", "af_nicole", "af_aoede", "af_kore", "af_sarah",
    "af_sky", "af_nova", "af_alloy", "af_jessica", "af_river",
    # American English — male
    "am_michael", "am_puck", "am_fenrir", "am_echo", "am_eric", "am_liam",
    "am_onyx", "am_adam", "am_santa",
    # British English — female then male
    "bf_emma", "bf_isabella", "bf_alice", "bf_lily",
    "bm_george", "bm_fable", "bm_daniel", "bm_lewis",
)

# Pronunciation overrides for words misaki/espeak get wrong — keyed by lowercased word, valued by
# misaki PHONEMES (misaki's alphabet, not raw IPA; e.g. Kokoro → "kˈOkəɹO"). Emitted as misaki
# inline overrides "[word](/phonemes/)" and applied ONLY on the misaki path (espeak can't read the
# markup). Empty by default — nothing is guessed; the owner adds an entry by running misaki on the
# box to get the word's phonemes, then dropping it here (see deploy/tts-stt/README.md). Case-
# insensitive whole-word match.
KOKORO_LEXICON: dict[str, str] = {
    # Titusville, FL — misaki/espeak stress the first vowel wrong; this says "TIGHT-us-vill"
    # (capital I = /aɪ/, same convention as the kˈOkəɹO example above). Verify by ear on the box
    # and re-derive with the README command if it drifts.
    "titusville": "tˈItəsvɪl",
}

# Curated NARRATOR blends: a blend id "kokoro-<key>" whose voice is a weighted average of real
# Kokoro voice style matrices — a custom timbre no single baked voice gives. Each key maps to
# ((voice, weight), …); weights should sum to ~1 (the style row is used directly, no
# renormalization). Owner-tunable by ear — retune the voices/weights or add blends. The referenced
# voices must be real English voices in the bin (a test guards they're in CURATED_KOKORO_VOICES).
KOKORO_BLENDS: dict[str, tuple[tuple[str, float], ...]] = {
    "narrator": (("am_michael", 0.6), ("af_nicole", 0.4)),
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

# Resident Kokoro engine (holds 0 or 1) — the ~310 MB model loads lazily on the first Kokoro
# render (outside _synth_lock, like piper's model load) and is reused thereafter. Its own load
# lock (NOT _synth_lock, so a cold load never blocks piper renders) makes the lazy init
# check-and-set atomic: without it two concurrent first-renders on a threaded server could each
# construct a model and leave two resident (piper's path self-dedupes via a dict; this can't).
_kokoro_holder: list[Any] = []
_kokoro_load_lock = threading.Lock()

# Resident misaki G2P (holds 0 or 1 — the G2P, or None once we've learned it's unavailable, so
# we don't retry the import every render). Loaded outside _synth_lock like the model; its own lock.
_g2p_holder: list[Any] = []
_g2p_load_lock = threading.Lock()


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
    """Installed selectable voice ids: piper voices (incl. curated multi-speaker entries) plus
    the curated Kokoro voices when Kokoro is baked. [] when nothing is installed. (Name kept for
    the /tts/voices seam its callers use; it now spans both engines.)"""
    return [vid for vid, _, _ in _voices()] + kokoro_voices()


def piper_speakers() -> dict[str, list[str]]:
    """For each installed MULTI-speaker model, its speaker names sorted by piper speaker index
    (ascending) — a stable, glanceable ordering the caller shuffles across, rebuilding the id
    "<stem>#<name>" from a name (which `_resolve_voice` maps back to the real index, so the
    list position is only a display counter, not the synthesis id). The Settings voice explorer
    uses this to audition all 900-odd libritts_r speakers, not just the curated few. {} when no
    installed model is multi-speaker."""
    out: dict[str, list[str]] = {}
    for stem, model in sorted(_voice_models().items()):
        smap = _speaker_map(model)
        if len(smap) <= 1:
            continue
        out[stem] = [name for name, _ in sorted(smap.items(), key=lambda kv: kv[1])]
    return out


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
    # Not a curated preset — accept any "<stem>#<name>" whose speaker exists in an installed
    # multi-speaker model, so the voice explorer can render any of its speakers (not only the
    # curated few). <name> is validated against the model's speaker_id_map (a finite set read
    # from the model file), so this never yields an out-of-range speaker or a traversal path.
    if "#" in voice_id:
        stem, name = voice_id.split("#", 1)
        model = _voice_models().get(stem)
        if model is not None:
            index = _speaker_map(model).get(name)
            if index is not None:
                return model, index
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


def _pad(wav_bytes: bytes, lead_ms: int, trail_ms: int) -> bytes:
    """Prepend `lead_ms` and append `trail_ms` of silence to a WAV (stdlib only). The lead lets a
    cold audio sink clip silence not the first word; the trail is audiobook pacing — a beat between
    clips. No-op (returns the input) when both are ≤ 0."""
    if lead_ms <= 0 and trail_ms <= 0:
        return wav_bytes
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            params = w.getparams()
            frames = w.readframes(w.getnframes())
        frame = params.sampwidth * params.nchannels
        lead = b"\x00" * (int(params.framerate * max(lead_ms, 0) / 1000) * frame)
        trail = b"\x00" * (int(params.framerate * max(trail_ms, 0) / 1000) * frame)
        out = io.BytesIO()
        with wave.open(out, "wb") as o:
            o.setparams(params)
            o.writeframes(lead + frames + trail)
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


def _kokoro_available() -> bool:
    """True when both Kokoro weight files are baked on disk (gates listing AND render)."""
    return (KOKORO_DIR / KOKORO_MODEL).exists() and (KOKORO_DIR / KOKORO_VOICES_FILE).exists()


def kokoro_voices() -> list[str]:
    """The curated Kokoro voice ids ("kokoro-<voice>"), but only when the weights are baked — so
    a box without them lists no Kokoro voices instead of dead entries. [] otherwise."""
    if not _kokoro_available():
        return []
    return [f"{KOKORO_ID_PREFIX}{v}" for v in CURATED_KOKORO_VOICES] + [
        f"{KOKORO_ID_PREFIX}{k}" for k in KOKORO_BLENDS
    ]


def _load_kokoro() -> Any:
    """Return the resident Kokoro engine, loading (and caching) it on first use. The load is the
    expensive step (~310 MB model); every render after reuses it. Imported lazily — the
    `kokoro_onnx` package lives only in the tts-stt image, not the app/test venv."""
    if not _kokoro_holder:
        with _kokoro_load_lock:
            if not _kokoro_holder:  # re-check: a racing caller may have loaded it while we waited
                from kokoro_onnx import Kokoro

                _kokoro_holder.append(
                    Kokoro(str(KOKORO_DIR / KOKORO_MODEL), str(KOKORO_DIR / KOKORO_VOICES_FILE))
                )
    return _kokoro_holder[0]


def _floats_to_wav(samples: Any, sample_rate: int) -> bytes:
    """Pack float samples in [-1, 1] into a mono 16-bit PCM WAV — stdlib only, so numpy (which
    ships inside the kokoro package) is never imported here. Clamps before scaling so a sample
    slightly past ±1 can't wrap to the opposite rail."""
    pcm = array("h", (max(-32768, min(32767, int(s * 32767))) for s in samples))
    if sys.byteorder == "big":  # WAV frames are little-endian
        pcm.byteswap()
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return out.getvalue()


def _load_g2p() -> Any:
    """The resident misaki English G2P — better English than espeak (POS-based homographs,
    num2words) with an espeak fallback for OOV — loaded once. Returns None when misaki isn't
    installed or fails to load, so the caller lets Kokoro phonemize with its built-in espeak.
    Non-fatal: a misaki problem disables the upgrade, never a broken render. trf=False keeps it on
    the small en_core_web_sm spaCy model (bounded RAM). The load runs outside _synth_lock."""
    if not _g2p_holder:
        with _g2p_load_lock:
            if not _g2p_holder:
                try:
                    from misaki import en, espeak

                    fallback = espeak.EspeakFallback(british=False)
                    _g2p_holder.append(en.G2P(trf=False, british=False, fallback=fallback))
                    print("[tts] misaki G2P loaded for Kokoro", file=sys.stderr)
                except Exception as exc:  # noqa: BLE001 — misaki is optional; degrade to espeak
                    print(
                        f"[tts] misaki G2P unavailable, Kokoro will use espeak: "
                        f"{type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                    _g2p_holder.append(None)
    return _g2p_holder[0]


def _apply_lexicon(text: str) -> str:
    """Wrap each KOKORO_LEXICON word in misaki's inline override "[word](/phonemes/)" so the G2P
    speaks it our way. No-op when the lexicon is empty (the default) — misaki+espeak handle the
    rest. Only meaningful on the misaki path (espeak can't read the markup)."""
    if not KOKORO_LEXICON:
        return text
    alternation = "|".join(re.escape(w) for w in KOKORO_LEXICON)
    pattern = re.compile(rf"\b(?:{alternation})\b", re.IGNORECASE)
    return pattern.sub(
        lambda m: (
            f"[{m.group(0)}](/{KOKORO_LEXICON[m.group(0).lower()]}/)"
            if m.group(0).lower() in KOKORO_LEXICON
            else m.group(0)
        ),
        text,
    )


def _blend_style(kokoro: Any, blend: tuple[tuple[str, float], ...]) -> Any:
    """Weighted average of named Kokoro voice style matrices → a blended voice style, passed to
    create() as an array (which indexes it by token length exactly like a named voice). numpy-free
    here — the style arrays' own * / + operators do the work, so this module stays out of numpy."""
    style: Any = None
    for vname, weight in blend:
        contrib = kokoro.get_voice_style(vname) * weight
        style = contrib if style is None else style + contrib
    return style


def _kokoro_wav(
    text: str,
    voice_id: str,
    lead_ms: int | None,
    speed: float | None = None,
    trail_ms: int | None = None,
) -> bytes | None:
    """Render `text` with the warm Kokoro engine for a "kokoro-<voice>" id. Dispatched BEFORE
    the piper resolver so a Kokoro id can never hit piper's unknown-id fallback and render in
    the first piper voice (a silent wrong-voice). None (→ device native) when Kokoro isn't baked
    or synth fails — every None path logs. The model LOAD runs outside _synth_lock (a
    multi-second one-time cost); only the synth call is serialised (onnxruntime isn't safe to
    Run concurrently on one session). `speed`/`trail_ms` default to the KOKORO_SPEED/TRAIL_MS env
    (audiobook pacing); `speed` is clamped here, `trail_ms` is bounded by the /tts handler / env."""
    name = voice_id[len(KOKORO_ID_PREFIX) :]
    spd = max(0.5, min(2.0, KOKORO_SPEED if speed is None else speed))
    if not _kokoro_available():
        print(f"[tts] render failed for {voice_id!r}: kokoro not installed", file=sys.stderr)
        return None
    if _tts_debug[0]:
        print(f"[tts] rendering {voice_id!r} -> kokoro voice={name} ({len(text)} chars)",
              file=sys.stderr)
    started = time.monotonic()
    try:
        kokoro = _load_kokoro()
        g2p = _load_g2p()  # None → let Kokoro phonemize with its built-in espeak
        # A blend id resolves to a computed style array; a normal voice passes its name string.
        blend = KOKORO_BLENDS.get(name)
        voice_arg: Any = _blend_style(kokoro, blend) if blend else name
        with _synth_lock:
            samples: Any = None
            if g2p is not None:
                try:
                    phonemes, _tokens = g2p(_apply_lexicon(text))
                    samples, sample_rate = kokoro.create(
                        phonemes, voice=voice_arg, speed=spd, is_phonemes=True
                    )
                except Exception as exc:  # noqa: BLE001 — a G2P hiccup degrades to espeak, not silence
                    print(f"[tts] misaki phonemize failed for {voice_id!r}, using espeak: "
                          f"{type(exc).__name__}: {exc}", file=sys.stderr)
                    samples = None
            if samples is None:  # no misaki, or it just failed — Kokoro's built-in espeak
                samples, sample_rate = kokoro.create(text, voice=voice_arg, speed=spd, lang="en-us")
        data = _floats_to_wav(samples, int(sample_rate))
    except Exception as exc:  # noqa: BLE001 — any synth failure must surface, not crash the server
        print(f"[tts] render failed for {voice_id!r} (kokoro): {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return None
    if _tts_debug[0]:
        ms = int((time.monotonic() - started) * 1000)
        print(f"[tts] rendered {voice_id!r} (kokoro): {len(data)} bytes in {ms} ms",
              file=sys.stderr)
    lead = PIPER_LEAD_MS if lead_ms is None else lead_ms
    trail = KOKORO_TRAIL_MS if trail_ms is None else trail_ms
    return _pad(data, lead, trail)


# --- Speakable-text normalization (engine-agnostic; runs before piper OR Kokoro) ------------
# Expands symbols/abbreviations an answer writes tersely but a voice should SPEAK in full. Distinct
# from KOKORO_LEXICON (which fixes single-word PHONEMES on the misaki path only): these are plain
# text rewrites, so both engines benefit. Extend the maps below to cover a new symbol/abbreviation.

# US Postal state codes -> spoken name, applied ONLY in the "City, ST" shape (a comma + a
# Capitalized word before the code) so a bare "IN"/"OR"/"ME" — real English words — is never
# touched outside that location signal. Heuristic, not perfect: "Yes, OK" would expand too; the
# comma+Capitalized-word gate keeps false positives rare in answer text.
_STATE_NAMES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "DC": "D.C.",
}

# 16-point compass. The 2- and 3-letter codes are essentially never non-compass in an answer, so
# they expand wherever they stand alone; bare N/S/E/W are ambiguous (grades, initials) so those
# expand only right after "from"/"the" — how a wind reading reads ("from the W").
_COMPASS: dict[str, str] = {
    "NNE": "north northeast", "ENE": "east northeast", "ESE": "east southeast",
    "SSE": "south southeast", "SSW": "south southwest", "WSW": "west southwest",
    "WNW": "west northwest", "NNW": "north northwest",
    "NE": "northeast", "SE": "southeast", "SW": "southwest", "NW": "northwest",
}
_CARDINAL: dict[str, str] = {"N": "north", "S": "south", "E": "east", "W": "west"}

# Degree units ("94 °F" / "94°F" -> "94 degrees Fahrenheit"; a lone "°" -> "degrees") and wind
# speeds ("4 mph" -> "4 miles per hour").
_DEGREE_UNITS: dict[str, str] = {"F": "Fahrenheit", "C": "Celsius", "K": "Kelvin"}
_SPEED_UNITS: dict[str, str] = {"mph": "miles per hour", "kph": "kilometers per hour",
                                "kmh": "kilometers per hour"}

_DEGREE_RE = re.compile(r"°\s*([FCK])\b")
# City word + comma + spaces captured as group 1 (kept verbatim), the code as group 2.
_STATE_RE = re.compile(
    r"([A-Z][A-Za-z.'\-]*,[ \t]+)(" + "|".join(_STATE_NAMES) + r")(?=[\s.,;:!?)]|$)"
)
_SPEED_RE = re.compile(r"\b(mph|km/?h|kph)\b", re.IGNORECASE)
# Distance "mi" -> "miles", gated to a preceding number ("40 mi" -> "40 miles") so the word is
# never invented from a stray "mi". Only the wall reaches this with digits intact; the PWA already
# expands it in speakable.js before the box sees it.
_DISTANCE_RE = re.compile(r"\b(\d[\d,]*(?:\.\d+)?)\s*mi\b")
# 3-letter codes first so "SSW" matches whole, not as "S" + "SW".
_COMPASS_RE = re.compile(r"\b(" + "|".join(sorted(_COMPASS, key=len, reverse=True)) + r")\b")
_CARDINAL_RE = re.compile(r"\b([Ff]rom|[Tt]he)\s+([NSEW])\b")

# Dates: "July 10, 2026" / "July 10 2026" / "July 10th" -> "July tenth, twenty twenty six". Spelled
# out here so BOTH engines say the day as an ORDINAL and the year in speech style, which neither
# number reader does on its own (misaki/espeak would say "ten" and "two thousand twenty-six").
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December")
_CARD_ONES = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
              "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
              "eighteen", "nineteen")
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty", 7: "seventy", 8: "eighty",
         9: "ninety"}
_ORD_ONES = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth",
             7: "seventh", 8: "eighth", 9: "ninth", 10: "tenth", 11: "eleventh", 12: "twelfth",
             13: "thirteenth", 14: "fourteenth", 15: "fifteenth", 16: "sixteenth",
             17: "seventeenth", 18: "eighteenth", 19: "nineteenth", 20: "twentieth", 30: "thirtieth"}
_DATE_RE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b"
)


def _two_digit_words(n: int) -> str:
    """Cardinal words for 0-99 ("26" -> "twenty six")."""
    if n < 20:
        return _CARD_ONES[n]
    tens, ones = divmod(n, 10)
    return _TENS[tens] + (f" {_CARD_ONES[ones]}" if ones else "")


def _ordinal_day(n: int) -> str:
    """Ordinal words for a day 1-31 ("10" -> "tenth", "21" -> "twenty first")."""
    if n in _ORD_ONES:
        return _ORD_ONES[n]
    tens, ones = divmod(n, 10)
    return f"{_TENS[tens]} {_ORD_ONES[ones]}"


def _year_words(y: int) -> str:
    """A 4-digit year in speech style: 2026 -> "twenty twenty six", 1999 -> "nineteen ninety nine",
    2000 -> "two thousand", 2005 -> "two thousand five", 1905 -> "nineteen oh five"."""
    hi, lo = divmod(y, 100)
    if lo == 0:
        return f"{_two_digit_words(y // 1000)} thousand" if y % 1000 == 0 else \
            f"{_two_digit_words(hi)} hundred"
    if lo < 10:
        return f"two thousand {_CARD_ONES[lo]}" if 2000 <= y < 2010 else \
            f"{_two_digit_words(hi)} oh {_CARD_ONES[lo]}"
    return f"{_two_digit_words(hi)} {_two_digit_words(lo)}"


def _date_sub(m: "re.Match[str]") -> str:
    day = int(m.group(2))
    if not 1 <= day <= 31:  # not a day-of-month — leave the text untouched
        return m.group(0)
    out = f"{m.group(1)} {_ordinal_day(day)}"
    return f"{out}, {_year_words(int(m.group(3)))}" if m.group(3) else out


def _speakable_text(text: str) -> str:
    """Rewrite terse symbols/abbreviations to their spoken words before phonemizing. Ordered so a
    state ("Omaha, NE") is expanded before the compass pass could read its "NE" as "northeast"."""
    text = _DATE_RE.sub(_date_sub, text)
    text = _DEGREE_RE.sub(lambda m: f" degrees {_DEGREE_UNITS[m.group(1)]}", text)
    text = text.replace("°", " degrees")
    text = _STATE_RE.sub(lambda m: m.group(1) + _STATE_NAMES[m.group(2)], text)
    text = _SPEED_RE.sub(lambda m: _SPEED_UNITS[m.group(1).lower().replace("/", "")], text)
    text = _DISTANCE_RE.sub(r"\1 miles", text)
    text = _COMPASS_RE.sub(lambda m: _COMPASS[m.group(1)], text)
    text = _CARDINAL_RE.sub(lambda m: f"{m.group(1)} {_CARDINAL[m.group(2)]}", text)
    return re.sub(r"[ \t]{2,}", " ", text)  # collapse a double space an expansion left behind


def tts_wav(
    text: str,
    voice: str,
    lead_ms: int | None = None,
    speed: float | None = None,
    trail_ms: int | None = None,
) -> bytes | None:
    """Render `text` to a WAV with the warm piper model for `voice` (a voice id validated
    against the installed set — no path traversal). For a multi-speaker voice
    ("<stem>#<speaker>") the resolved index is passed as the synthesis speaker_id. None
    when TTS isn't available or rendering fails; every None path is logged (a silent None
    degrades the reply to the device's native voice, which looks like the wrong voice).
    `speed`/`trail_ms` are the audiobook-pacing controls — Kokoro honours both; piper ignores
    `speed` (its synthesizer isn't speed-controlled on this path) and honours only an EXPLICIT
    `trail_ms` (its env default is 0, since the snappy piper is the fallback voice, not the
    audiobook one)."""
    text = _speakable_text(text)  # expand °F/mph/compass/"City, ST" for BOTH engines, pre-dispatch
    if voice.startswith(KOKORO_ID_PREFIX):
        return _kokoro_wav(text, voice, lead_ms, speed, trail_ms)
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
    return _pad(data, lead, trail_ms or 0)  # piper: only an explicit trail; no audiobook default


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
        elif path == "/tts/speakers":
            self._send(200, json.dumps({"speakers": piper_speakers()}).encode(), "application/json")
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
            speed = None
            try:
                raw = qs.get("speed", [None])[0]
                if raw is not None:
                    speed = max(0.5, min(2.0, float(raw)))
            except (TypeError, ValueError):
                speed = None
            trail_ms = None
            try:
                raw = qs.get("trail", [None])[0]
                if raw is not None:
                    trail_ms = max(0, min(3000, int(raw)))
            except (TypeError, ValueError):
                trail_ms = None
            wav = tts_wav(text, qs.get("voice", [""])[0], lead_ms, speed, trail_ms)
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
