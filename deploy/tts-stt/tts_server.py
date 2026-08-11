#!/usr/bin/env python3
"""Warm-model Kokoro TTS server for the JBrain2 `tts-stt` speech service.

The box's read-aloud voice: renders answer text to WAV with Kokoro-82M and returns the
audio. The ~310 MB Kokoro model loads once (lazily, on the first render) and is held
resident so each clip renders fast instead of cold-loading the model every time. It
shares the `tts-stt` container with the whisper.cpp STT server (llama-swap, a separate
process on :8080); this is the TTS half, on :8801.

Reached over the internal docker network only (no LAN port): the authenticated api
proxies it for the PWA read-aloud + Settings sample (`/api/brain/tts`,
`/api/brain/voices`), and the `wall` display forwards its own read-aloud here. It
touches NO database and NO user data — only the answer TEXT the owner asked to be
read, rendered to audio and returned; nothing is stored.

Kokoro is ONE onnx model + a voice-styles bin that together serve many voices, each a
selectable id "kokoro-<voice>". Phonemization goes through misaki (better English than
espeak, and the ONLY path on which the KOKORO_LEXICON pronunciation overrides apply); when
misaki is unavailable Kokoro falls back to its own built-in espeak — GET /tts/health reports
which. When Kokoro itself isn't provisioned the box lists no voices and read-aloud falls back
to the caller's own device voice (there is no other on-box engine — piper was removed).

Stdlib only — no web framework, no piper.
"""

from __future__ import annotations

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

# A short lead of silence so a cold audio-sink resume clips the silence, not the first
# word (the page's WebAudio keep-alive is the real fix; this is a backstop). Requested
# only on the FIRST clip of a turn — continuation clips send ?lead=0 for gapless playback.
LEAD_MS = int(os.environ.get("BRAIN_TTS_LEAD_MS", "400"))
# Longest single render the page requests — it splits a reply into sentence-sized clips,
# so this bounds one chunk, not the whole answer.
TTS_CHUNK_CAP = 1000

# --- Kokoro-82M: the on-box TTS engine (Apache-2.0) -----------------------------------------
# Kokoro is ONE onnx model + a voice-styles bin that together serve many voices, all under the
# one KOKORO_DIR. We surface a curated set as ids "kokoro-<voice>", selectable ONLY when the
# weights are baked on disk — a box without them lists no voices (read-aloud then uses the
# device voice) rather than dead entries. Keep KOKORO_MODEL/KOKORO_VOICES_FILE in step with the
# bake block in Dockerfile.tts-stt (the test guards this).
KOKORO_DIR = Path(os.environ.get("BRAIN_KOKORO_DIR", "/opt/kokoro"))
KOKORO_MODEL = "kokoro-v1.0.onnx"
KOKORO_VOICES_FILE = "voices-v1.0.bin"
KOKORO_ID_PREFIX = "kokoro-"
# Audiobook pacing (env-tunable; both default to NO-OP so chat answers are unchanged — the owner
# dials them in by ear after a listen). SPEED < 1.0 reads slower/warmer; TRAIL_MS appends silence
# after each Kokoro clip for a beat between sentences. Per-request `speed`/`trail` on /tts override
# these (for the future story-vs-answer modes). Kokoro only; clamped at use. Parsed defensively so a
# typo'd env can't crash the always-on TTS service.
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

# A lock serialising synthesis (onnxruntime is not safe to Run concurrently on one session —
# renders are fast, so a single global lock is simplest and cheap).
_synth_lock = threading.Lock()

# Resident Kokoro engine (holds 0 or 1) — the ~310 MB model loads lazily on the first render
# (outside _synth_lock) and is reused thereafter. Its own load lock (NOT _synth_lock, so a cold
# load never blocks a render) makes the lazy init check-and-set atomic: without it two concurrent
# first-renders on a threaded server could each construct a model and leave two resident.
_kokoro_holder: list[Any] = []
_kokoro_load_lock = threading.Lock()

# Resident misaki G2P (holds 0 or 1 — the G2P, or None once we've learned it's unavailable, so
# we don't retry the import every render). Loaded outside _synth_lock like the model; its own lock.
_g2p_holder: list[Any] = []
_g2p_load_lock = threading.Lock()


def tts_voices() -> list[str]:
    """Installed selectable voice ids — the curated Kokoro voices when the weights are baked, []
    otherwise (read-aloud then falls back to the device voice). Named for the /tts/voices seam its
    callers use."""
    return kokoro_voices()


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
    """Render `text` with the warm Kokoro engine for a "kokoro-<voice>" id (already normalized by
    tts_wav to a valid id, so an unknown name never reaches the engine). None (→ device native)
    when Kokoro isn't baked or synth fails — every None path logs. The model LOAD runs outside
    _synth_lock (a
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
    lead = LEAD_MS if lead_ms is None else lead_ms
    trail = KOKORO_TRAIL_MS if trail_ms is None else trail_ms
    return _pad(data, lead, trail)


def tts_health() -> dict[str, Any]:
    """A readiness snapshot the app surfaces so a SILENT espeak fallback is visible, not guessed.
    Kokoro phonemizes through misaki when its spaCy G2P loaded, else through kokoro-onnx's built-in
    espeak — and the KOKORO_LEXICON phoneme overrides apply ONLY on the misaki path, so an espeak
    fallback silently un-fixes every seeded word (e.g. "Titusville"). `g2p` is "unavailable" when
    Kokoro isn't baked at all, else "misaki"/"espeak" for what a render WOULD use right now. Loading
    the G2P is the same lazy, cached, non-fatal step a first Kokoro render triggers, so hitting this
    also pre-warms misaki."""
    kokoro = _kokoro_available()
    g2p = "unavailable"
    if kokoro:
        g2p = "misaki" if _load_g2p() is not None else "espeak"
    return {
        "kokoro_available": kokoro,
        "g2p": g2p,
        "lexicon_entries": len(KOKORO_LEXICON),
        "voice_count": len(tts_voices()),
    }


# --- Speakable-text normalization (runs before Kokoro phonemizes) ---------------------------
# Expands symbols/abbreviations an answer writes tersely but a voice should SPEAK in full. Distinct
# from KOKORO_LEXICON (which fixes single-word PHONEMES on the misaki path only): these are plain
# text rewrites, so both engines benefit. Extend the maps below to cover a new symbol/abbreviation.

# Curly "smart" quotes -> ASCII. misaki mis-tokenizes a contraction/possessive written with a curly
# apostrophe (U+2019): "Here's" phonemizes as "here ess" (the clitic 's' becomes the LETTER name),
# where a straight ' reads as the "-z" contraction. Some words (it's/today's) survive in misaki's
# lexicon and some don't, so normalize unconditionally — every downstream rule and BOTH engines
# (misaki + espeak) then see plain quotes. This is the single chokepoint all render text passes
# through, so it fixes the PWA and wall paths alike. Doubles are folded too (harmless, same intent).
_SMART_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})

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
# Dotted initialisms ("U.S.", "U.K.", "D.C.") -> spaced letters ("U S"), so their interior periods
# aren't read as a sentence end (the obnoxious pause) and don't split a clip. Uppercase, >=2 groups;
# runs BEFORE the name-initial + acronym passes. The PWA's speakable.js does this too — here it
# serves the wall (which sends "U.S." intact through its own mdToPlain).
_DOTTED_INITIALISM_RE = re.compile(r"\b(?:[A-Z]\.){2,}")
# A single-letter name initial ("Dennis E. Taylor") followed by a capitalized word: drop the period
# so espeak doesn't read it as a sentence end (a long pause). The dotted initialisms above are
# already collapsed. The PWA's speakable.js does this too; here it serves the wall.
_INITIAL_RE = re.compile(r"(?<!\.)\b([A-Z])\.(?=\s+[A-Z])")
# Relations whose glyphs read badly or drop silently (inverting a sentence's meaning) -> words.
# Composite forms before the bare < / >. The box doesn't verbalize numbers, so operands stay digits
# for misaki. Mirrors speakable.js's SYMBOL_WORDS inequality block for the wall path.
_RELATION_SUBS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\s*(?:<=|≤)\s*"), " less than or equal to "),
    (re.compile(r"\s*(?:>=|≥)\s*"), " greater than or equal to "),
    (re.compile(r"\s*(?:!=|≠)\s*"), " not equal to "),
    (re.compile(r"\s*±\s*"), " plus or minus "),
    (re.compile(r"\s*<\s*"), " less than "),
    (re.compile(r"\s*>\s*"), " greater than "),
)

# Acronyms/initialisms read as letters ("ORFS" -> "O R F S", "AI" stays 2-letter and is left alone).
# A standalone run of 3-5 uppercase letters, EXCEPT ones said as a word (NASA), common roman numerals
# (VII), or all-caps words used for emphasis/dialogue (STOP) — those are stoplisted. 2-letter runs are
# left alone (too many are ordinary words/codes: IN, US, OK, NO). Heuristic — extend the stoplist by
# ear. Runs LAST so compass/state codes (SSW, FL) are already words before this sees them.
_SPOKEN_ACRONYMS = frozenset({
    "NASA", "NATO", "RADAR", "LASER", "SCUBA", "SONAR", "ASCII", "AWOL",
    "YES", "HEY", "WOW", "NOW", "STOP", "HELP", "WAIT", "OKAY", "HELLO", "DAMN",
    "III", "VII", "VIII", "XII", "XIII", "XIV", "XVI", "XVII", "XVIII", "XIX",
})
_ACRONYM_RE = re.compile(r"\b([A-Z]{3,5})\b")

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
    text = text.translate(_SMART_QUOTES)  # curly ’ → ' so misaki reads "Here’s", not "here ess"
    text = _DATE_RE.sub(_date_sub, text)
    text = _DEGREE_RE.sub(lambda m: f" degrees {_DEGREE_UNITS[m.group(1)]}", text)
    text = text.replace("°", " degrees")
    text = _STATE_RE.sub(lambda m: m.group(1) + _STATE_NAMES[m.group(2)], text)
    text = _SPEED_RE.sub(lambda m: _SPEED_UNITS[m.group(1).lower().replace("/", "")], text)
    text = _DISTANCE_RE.sub(r"\1 miles", text)
    text = _COMPASS_RE.sub(lambda m: _COMPASS[m.group(1)], text)
    text = _CARDINAL_RE.sub(lambda m: f"{m.group(1)} {_CARDINAL[m.group(2)]}", text)
    text = _DOTTED_INITIALISM_RE.sub(lambda m: m.group(0).replace(".", " ").strip(), text)
    text = _INITIAL_RE.sub(r"\1", text)
    text = _ACRONYM_RE.sub(
        lambda m: m.group(1) if m.group(1) in _SPOKEN_ACRONYMS else " ".join(m.group(1)), text
    )
    for pat, word in _RELATION_SUBS:
        text = pat.sub(word, text)
    return re.sub(r"[ \t]{2,}", " ", text)  # collapse a double space an expansion left behind


def _resolve_kokoro_voice(voice: str) -> str:
    """Normalize a requested voice id to a valid "kokoro-<voice>" id, so a stale/blank/unknown id
    (e.g. an old piper id still stored on a client) renders in the default voice instead of a silent
    engine error. A bare or unprefixed id is treated as the default; an unknown Kokoro name likewise
    falls back — the exposed roster (kokoro_voices) is the finite valid set."""
    default = f"{KOKORO_ID_PREFIX}{CURATED_KOKORO_VOICES[0]}"
    if not voice.startswith(KOKORO_ID_PREFIX):
        return default
    name = voice[len(KOKORO_ID_PREFIX) :]
    return voice if (name in CURATED_KOKORO_VOICES or name in KOKORO_BLENDS) else default


def tts_wav(
    text: str,
    voice: str,
    lead_ms: int | None = None,
    speed: float | None = None,
    trail_ms: int | None = None,
) -> bytes | None:
    """Render `text` to a WAV in the requested Kokoro `voice` and return the audio. `voice` is
    normalized to a valid "kokoro-<voice>" id (a stale/unknown id falls back to the default voice)
    so a bad id never traverses to a wrong render. None when Kokoro isn't provisioned or the render
    fails; every None path is logged (a silent None degrades the reply to the device's native
    voice). `speed`/`trail_ms` are the audiobook-pacing controls (both env-defaulted, per-request
    override, clamped in _kokoro_wav / the /tts handler)."""
    text = _speakable_text(text)  # expand °F/mph/compass/"City, ST" before phonemizing
    return _kokoro_wav(text, _resolve_kokoro_voice(voice), lead_ms, speed, trail_ms)


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
            self._send(200, json.dumps({"voices": tts_voices()}).encode(), "application/json")
        elif path == "/tts/health":
            self._send(200, json.dumps(tts_health()).encode(), "application/json")
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
    server = ThreadingHTTPServer((host, port), Handler)
    installed = ", ".join(tts_voices()) or "none (kokoro not provisioned — device voice fallback)"
    print(f"kokoro tts server on http://{host}:{port}/  (voices: {installed})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
