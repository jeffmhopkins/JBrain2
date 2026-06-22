# analyze_video — build plan

On-box video understanding: jerv (and the note pipeline, later) can read a video
attachment by **sampling frames → captioning each with the vision LLM → transcribing
the audio (whisper) → fusing both on a timeline → summarizing**. The summary +
per-frame data are stored and made searchable, and an owner-facing card lets you
scrub the video with the AI analysis surfaced inline.

This is the **map-reduce over a text bottleneck** paradigm the research converged on
(raw frames as VLM tokens explode context; captioning to text early keeps cost and
memory bounded). See the design research in this PR's history for prior art
(Gemini/GPT-4o sample frames + audio; LiveCC interleaves ASR with frames by
timestamp; tree-of-captions hierarchical summary).

## Owner decisions (locked)

- **Run shape:** a background **job** (`analyze_video_attachment`, sibling of
  `transcribe_attachment`/`ocr_attachment`) writes a cached result; the
  `analyze_video` agent tool kicks it and renders from cache (graceful "analyzing…"
  on a miss, the OCR on-demand pattern). Heavy, observable, re-viewable, searchable.
- **Frame extraction:** runs **in the worker** via the system **ffmpeg/ffprobe**
  (added to the backend image + `scripts/dev-setup.sh` + CI). No heavy decode lib;
  Pillow only computes the dedup hash.
- **Sampling:** **K evenly-spaced frames** (≈ every `100/K`%), capped (`max_frames`,
  default 24), downscaled to a 768px longest edge, **near-duplicate (dHash) deduped**
  so static stretches don't spend the budget. Scene-detection is a possible v2.
- **Map granularity:** **per-frame captions** (parallelizable, model-agnostic, and
  gives the timeline component its per-frame data) + a single **reduce** summary call.
- **Card:** a materially new GUI surface → goes through the **3-mock GUI gate**
  (docs/PROCESS.md) before the component is built.

## Waves

### Wave 1 — Frame extraction (this PR)
`jbrain.media`: `ffmpeg_available()`, `probe_duration_s()`, and `sample_frames()`
(probe → `fps = max_frames/duration` → ffmpeg `fps,scale` → ms-stamped JPEGs → dHash
dedup). ffmpeg added to `backend/Dockerfile`, `scripts/dev-setup.sh`, and the CI
backend job. Unit tests generate synthetic clips with ffmpeg and skip when it's
absent.

### Wave 2 — The job (map → fuse → reduce)
`analyze_video_attachment` `ActionSpec` (cost_class `expensive`, dedup
`attachment_id`). Map each kept frame → `router.complete("agent.vision", …)` caption;
audio → the existing whisper path; fuse captions + transcript on one `[mm:ss]`
timeline; reduce → a summary. Persist an `AttachmentExtract(kind="video_analysis")`
— `text` = summary (searchable), structured per-frame `{t, caption, thumb_id}` +
transcript in the JSONB column, confidence capped (~0.6, Guards) — plus the frame
thumbnails as blobs. Re-enqueue `ingest_note`. Real-Postgres + RLS isolation test.

### Wave 3 — The tool
`analyze_video.tool` sidecar + handler: read the cache; on a miss, enqueue the job
and return "analyzing… check back". Returns the summary text + a `video_analysis`
ViewPayload (attachment id + structured analysis; no URLs — invariant #9). Optional-
tool/graceful-degrade wiring + digest pin.

### Wave 4 — The scrubbing/timeline card (after the mock gate)
`<video controls>` + a summary panel + a timeline rail of frame-thumbnail markers;
scrubbing/playback surfaces the active frame's caption and the spoken words (reusing
the transcript karaoke logic); tap a marker to seek. Renders from the stored
structure.

## Defaults (research-backed)
- `max_frames = 24`, longest edge `768`, dedup Hamming distance `6/64`.
- Unknown duration → 1 fps capped at `max_frames`.
- Long/static content can later drop to a lower rate; scene-detection is a v2 option.

## Status
- **Wave 1 — in progress** (this PR: `jbrain.media` + ffmpeg wiring + tests).
- Waves 2–4 — not started.
