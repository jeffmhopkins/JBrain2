# tts-stt — the box's speech service

One always-on container serving the box's **speech I/O**:

- **TTS (`:8801`)** — `piper_server.py`, a stdlib HTTP server that renders read-aloud with
  **piper**, holding each voice's model **resident** (`piper.voice.PiperVoice`) so a clip
  renders in ~0.1 s instead of cold-loading the model every call (~1.5 s — the old
  subprocess-per-clip cost). Serves `/tts`, `/tts/voices`, `/tts/speakers`, `/tts/silence`,
  `/healthz`, and latches the `tts_debug` flag. Reached by the api's authenticated read-aloud
  proxy (`/api/brain/tts`, `/api/brain/voices`, `/api/brain/speakers`) and by the `wall`
  display's `/tts` forward. `/tts/voices` is the curated preset list; `/tts/speakers` is the
  **full multi-speaker roster** (speaker names ordered by piper index) that the Settings voice
  explorer shuffles across — `/tts` renders **any** valid `<stem>#<speaker>` of an installed
  model, not only the curated few, validated against the model's `speaker_id_map`.
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
compiles whisper.cpp's server, adds Python + `piper-tts`, and bakes the default voices
(Joe, Amy, multi-speaker libritts_r whose curated speaker **3922** is a second female voice)
into `/opt/piper-voices`. `piper_server.py` + `entrypoint.sh` are **bind-mounted** at `/tts`
(like the wall's `serve.py`), so a `jbrain update` picks up code changes with no rebuild;
only a voice/base bump rebuilds. Extra voices drop into the mounted `voices/` dir.

## Env (compose)

`BRAIN_TTS_PORT` (8801) · `BRAIN_PIPER_VOICES_DIR` (`/tts/voices`, mounted extras) ·
`BRAIN_PIPER_BAKED_VOICES_DIR` (`/opt/piper-voices`) · `BRAIN_PIPER_PREWARM`
(`en_US-amy-medium` — pre-loaded at startup so the first clip isn't slow) ·
`BRAIN_PIPER_LEAD_MS` (silence pad on the first clip of a turn).

Diagnostics: while an owner debug-console token is live the api pushes `tts_debug` here, and
each render logs `[tts] rendering … speaker=N` / `[tts] rendered … in N ms` (failures always
log). Read via `docker logs tts-stt` or the debug console's `GET /api/debug/logs/tts-stt`.
