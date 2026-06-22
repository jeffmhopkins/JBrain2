# Whisper transcription — build plan

Add on-box speech-to-text (whisper.cpp) to JBrain2 in two roles:

1. **Attachment analyzer** — `audio/*` (and, fast-follow, `video/*` via ffmpeg)
   attachments are transcribed and indexed alongside note bodies, filling the
   `audio/*` slot `docs/ANALYSIS.md` already reserves (currently "deferred").
2. **Agent tool** — `jerv` can transcribe an attachment on demand mid-chat.

The model is **load-on-demand / unload-after**: it rides the existing on-box
**llama-swap gateway** (the `local-llm` compose profile built for this Strix Halo
box), which loads a model on first request and TTL-unloads it when idle. Both
roles additionally call `LocalGateway.unload(model)` when finished so VRAM is
freed immediately rather than waiting for the idle timeout.

This binds to `docs/PROCESS.md` (waves + independent review gate per wave) and the
`CLAUDE.md` non-negotiables.

## Architecture fit (grounded)

- **Not in-process.** No `torch`/`faster-whisper` in the worker. whisper.cpp runs
  in the gateway container, reached over HTTP — the `embed.py` (TEI) precedent.
- **Outside the LLM adapter.** Transcription is audio→text, not a completion, so
  like embeddings and `llm/local_gateway.py` it lives outside `LlmRouter`
  (rule 1 governs completions). Usage is not billed through `llm_usage`.
- **Async job, mirroring OCR.** Capture-to-searchable never waits on the model
  (`docs/ANALYSIS.md`), so audio transcription is an async `transcribe_attachment`
  job — the sibling of `ocr_attachment` — that writes the `attachment_extracts`
  cache and re-enqueues `ingest_note`. Ingest reads only the cache.
- **One small migration.** `transcript` is already a defined `Segment` kind and
  `chunks.source_kind` is unconstrained, but `attachment_extracts.kind` has an
  allowlist CHECK (migration 0011) — so migration 0079 admits `'transcript'`
  (no new table; rides the existing RLS policy + grants). The new job kind
  registers as an **in-code-only** action (added to the `build_registry` tuple in
  `worker.py`, like `eval_run`/`skill_*`/`wiki_*`), so the `app.actions` seed and
  its lockstep test are untouched.
- **Graceful disable.** Empty `whisper_url` disables the feature end to end (no
  client wired; audio attachments extract to nothing), mirroring `comfyui`.

## Waves

### Wave 1 — Transcription core (foundation)
- `config.py`: `whisper_enabled`, `whisper_url`, `whisper_model`, `whisper_timeout`,
  `whisper_max_bytes`.
- `transcribe.py`: `TranscribeClient` Protocol + `WhisperCppClient` (multipart POST
  to the gateway's OpenAI `/v1/audio/transcriptions`), `Transcript` result, and a
  fakeable seam (injected `httpx` transport) exactly like `TeiEmbedClient`.
- Unit tests with `httpx.MockTransport`.
- **Gate:** independent review. Local `ruff` + `pyright` + unit tests.

### Wave 2 — Consumers (parallel tasks)
- **2A — analyzer path:** `TranscribePipeline.transcribe_attachment` (mirrors
  `OcrPipeline`), `TRANSCRIPT_CONFIDENCE` cap, `queue.has_active_transcribe_for_note`,
  `IngestPipeline._enqueue_transcribe_jobs` folded into the analysis gate, the
  `_after_exhaustion` body-only fallback, worker wiring + in-code `ActionSpec`,
  `extract.py` audio routing note. Integration tests (`test_transcribe_pg.py`) incl.
  an **RLS isolation test** for transcript extract rows.
- **2B — agent tool:** `transcribe.tool` sidecar + handler + `build_*_handlers`
  factory bound in the agent `build_registry`, `permission: read`,
  `cost_class: expensive`. Unloads the model after. Tests.
- **Gate:** per-task review, then a wave-level review (touches RLS → red-team).

### Wave 3 — Deploy + docs
- `deploy/docker-compose.yml` + `scripts/local-llm-setup.sh`: register the
  whisper.cpp model in the llama-swap config; `scripts/dev-setup.sh` currency.
- `video/*` + ffmpeg extraction (fast-follow; new system dep flagged).
- Docs: flip `docs/ANALYSIS.md` `audio/*` from deferred to shipped; add the tool to
  `docs/ASSISTANT.md`; note the feature in `docs/ARCHITECTURE.md`/`ROADMAP.md`.

## Open decisions (resolved)
- Engine: **whisper.cpp via the existing llama-swap gateway** (owner's choice — best
  Strix Halo/Vulkan fit, reuses the proven load/unload admin API).
- PRs: the owner has not asked for PRs, so each wave is committed and pushed to the
  feature branch; PR-per-wave is offered, not opened (harness rule).
