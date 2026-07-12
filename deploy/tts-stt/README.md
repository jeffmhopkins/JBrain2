# tts-stt — the box's speech service

One always-on container serving the box's **speech I/O**:

- **TTS (`:8801`)** — `piper_server.py`, a stdlib HTTP server that renders read-aloud, holding
  each voice's model **resident** so a clip renders in ~0.1 s instead of cold-loading the model
  every call (~1.5 s — the old subprocess-per-clip cost). Serves two engines behind one seam:
  **piper** (`piper.voice.PiperVoice`, the default) and **Kokoro-82M** (`kokoro_onnx`, a more
  natural Apache-2.0 engine — one shared onnx model + a voice-styles bin serving many voices).
  Serves `/tts`, `/tts/voices`, `/tts/speakers`, `/tts/silence`, `/healthz`, and latches the
  `tts_debug` flag. Reached by the api's authenticated read-aloud proxy (`/api/brain/tts`,
  `/api/brain/voices`, `/api/brain/speakers`) and by the `wall` display's `/tts` forward.
  `/tts/voices` lists the curated piper presets **and** the Kokoro English voice roster (ids
  `kokoro-<voice>` — the American + British v1.0 voices, since read-aloud renders `lang=en-us`;
  appended after the piper ones and only when the Kokoro weights are baked, so a box without them
  lists none). In Settings, **Kokoro** is a first-class read-aloud model (Piper | Kokoro | Native);
  selecting it shows a dropdown of these voices. `/tts/speakers` is the **full multi-speaker piper
  roster** (speaker
  names ordered by piper index) that the Settings voice explorer shuffles across — for piper,
  `/tts` renders **any** valid `<stem>#<speaker>` of an installed model, not only the curated
  few, validated against the model's `speaker_id_map`. A `kokoro-<voice>` id is
  dispatched to Kokoro **before** the piper resolver, so it can never fall back to a piper
  voice; an unavailable Kokoro voice degrades to `None` (the device's native voice), never a
  silent wrong voice.
- **Kokoro pronunciation (misaki G2P).** Kokoro phonemizes through **misaki** (`_load_g2p`,
  `trf=False` → the small `en_core_web_sm` spaCy model to bound RAM) with an **espeak fallback**
  for out-of-vocabulary words — better English than espeak alone (POS-based homographs like
  "lead"/"read", `num2words`). misaki is **optional and non-fatal**: if it's not baked (or fails
  to load), the Kokoro path falls back to kokoro-onnx's built-in espeak, so read-aloud never
  breaks. To **fix a specific word**, add it to `KOKORO_LEXICON` in `piper_server.py` — key is the
  lowercased word, value is its **misaki phonemes** (misaki's alphabet, not raw IPA; derive them on
  the box with `python3 -c "from misaki import en, espeak; print(en.G2P(fallback=espeak.EspeakFallback())('the word')[0])"`). Entries are
  emitted as misaki inline overrides `[word](/phonemes/)` and applied only on the misaki path.
  **RAM:** misaki + its spaCy model add resident memory on top of Kokoro's ~310 MB — measure with
  `docker stats tts-stt` after a Kokoro render and record it here.
- **Speakable-text normalization (both engines).** Before *either* engine phonemizes, `tts_wav`
  runs `_speakable_text` to expand terse symbols/abbreviations an answer writes but a voice should
  speak in full: **degree units** (`94 °F` → "94 degrees Fahrenheit"; also °C/°K and a lone `°`),
  **wind speeds** (`4 mph`/`kph`/`km/h` → "miles/kilometers per hour"), **compass points** (the 2-
  and 3-letter codes anywhere — `SSW` → "south southwest"; bare `N/S/E/W` only after "from"/"the"
  so grades/initials aren't mangled), and **US state codes** in the `City, ST` shape (`Cocoa, FL`
  → "Cocoa, Florida") — gated to a comma + Capitalized word so a bare `IN`/`OR`/`ME` is left alone.
  This is plain-text rewriting (engine-agnostic, so piper benefits too) and is **separate** from
  `KOKORO_LEXICON`, which fixes single-word *phonemes* on the misaki path only. Add a symbol or
  abbreviation by extending the maps at the top of the `_speakable_text` block in `piper_server.py`
  (`_STATE_NAMES`, `_COMPASS`, `_DEGREE_UNITS`, `_SPEED_UNITS`).
- **STT (`:8080`)** — whisper.cpp behind llama-swap (load-on-demand, idle-unload). The api
  reaches it at `http://tts-stt:8080/v1`; the model + `llama-swap.yaml` config are
  provisioned by `jbrain enable-whisper` (`scripts/whisper-setup.sh`).

## Why colocated + default-on

Read-aloud (piper) must not depend on enabling STT, so both are **default-on** (the old
`whisper` compose profile is gone). The container's entrypoint runs **piper in the
foreground** (the always-needed half) and starts **whisper/llama-swap only when its config
exists** — so a fresh box without the whisper model still serves read-aloud instead of
crash-looping. piper is CPU; whisper uses the iGPU (`/dev/dri` + the video/render GIDs).

## Image + layout

`deploy/Dockerfile.tts-stt` builds on the llama-swap Vulkan image (llama-swap + ffmpeg),
compiles whisper.cpp's server, adds Python + `piper-tts` + `kokoro-onnx`, and bakes the voices:
the default piper voices (Joe, Amy, multi-speaker libritts_r whose curated speaker **3922** is a
second female voice) into `/opt/piper-voices`, and the **Kokoro-82M** model + voice-styles bin
(`kokoro-v1.0.onnx`, `voices-v1.0.bin`, ~340 MB, fetched with retry) into `/opt/kokoro` — its own
dir so the piper `*.onnx` glob never scans it. Adding a Kokoro voice needs no host step: it rides
the normal `docker compose build` in `jbrain update`, then appears as a `kokoro-<voice>` pick in
Settings. Keep the baked Kokoro filenames in step with `piper_server.py`'s
`KOKORO_MODEL`/`KOKORO_VOICES_FILE` (a unit test guards this), and the curated Kokoro voice ids
in `CURATED_KOKORO_VOICES`. `piper_server.py` + `entrypoint.sh` are **bind-mounted** at `/tts`
(like the wall's `serve.py`), so a `jbrain update` picks up code changes with no rebuild; only a
voice/base bump rebuilds. Extra piper voices drop into the mounted `voices/` dir.

## Env (compose)

`BRAIN_TTS_PORT` (8801) · `BRAIN_PIPER_VOICES_DIR` (`/tts/voices`, mounted extras) ·
`BRAIN_PIPER_BAKED_VOICES_DIR` (`/opt/piper-voices`) · `BRAIN_KOKORO_DIR` (`/opt/kokoro`, the
baked Kokoro model + voices bin) · `BRAIN_PIPER_PREWARM`
(`en_US-amy-medium` — pre-loaded at startup so the first clip isn't slow) ·
`BRAIN_PIPER_LEAD_MS` (silence pad on the first clip of a turn) · **`BRAIN_KOKORO_SPEED`** (1.0;
Kokoro read speed — set ~0.9 for a slower, warmer audiobook read) · **`BRAIN_KOKORO_TRAIL_MS`** (0;
silence appended after each Kokoro clip for a beat between sentences).

**Audiobook pacing.** `BRAIN_KOKORO_SPEED` / `BRAIN_KOKORO_TRAIL_MS` (and per-request `/tts?speed=`
/`?trail=`, clamped) tune Kokoro's pacing; both default to **no-op** so *markup* answers are
unchanged. They affect **Kokoro only** — the snappy piper fallback ignores the speed and the trail
env default. The PWA classifies each turn (`readingProfile`, markup vs prose) and **automatically**
sends a slower speed + inter-clip trail for **prose/stories** while leaving markup answers on the
env default — no user mode. The env vars set the markup/default read; the prose preset lives in
`useReadAloud` (`PROSE_SPEED`/`PROSE_TRAIL_MS`). Dial all of it in by ear after a listen.

**Narrator blends.** `KOKORO_BLENDS` in `piper_server.py` defines custom voices as a **weighted
average of two or more Kokoro voice style vectors** (a timbre no single baked voice gives) — each
appears in the Settings Kokoro list as `kokoro-<key>` ("Kokoro · Narrator"). Seeded with one warm
`narrator` blend (`am_michael` 0.6 + `af_nicole` 0.4); retune the voices/weights or add blends by
ear (weights should sum to ~1; referenced voices must be in `CURATED_KOKORO_VOICES` — a test
guards it). Blending is style-vector math on the resident model, so it needs no extra weights.

Diagnostics: while an owner debug-console token is live the api pushes `tts_debug` here, and
each render logs `[tts] rendering … speaker=N` / `[tts] rendered … in N ms` (failures always
log). Read via `docker logs tts-stt` or the debug console's `GET /api/debug/logs/tts-stt`.
