# Long media analysis — jobs, cancel-safe subprocesses, auto-resume

> **Status:** Scheduled · **Last verified:** 2026-07-18 · **Waves:** W1◻️ W2◻️ W3◻️

`analyze_stream` (and, for a long clip, `analyze_video`) currently runs its whole
pipeline — resolve → sample frames → caption each → transcribe → reduce — **inside the
interactive chat turn**. That fights the turn model three ways, all surfaced while
shipping the stream tool:

1. **Turn bounds.** A full-video pass is minutes long. It survives today only because
   the hard ceiling is generous (`_MAX_TURN_WALL_CLOCK_S = 3600`) and the chunked
   transcription keeps the **10-min idle watchdog** (`_TURN_IDLE_S = 600`) fed with
   per-chunk progress. It's close to the edge, not comfortably inside it.
2. **Stop doesn't kill the work.** The ffmpeg/whisper legs run via `subprocess.run`
   inside `asyncio.to_thread`. A thread can't be cancelled, so `POST /chat/runs/{id}/
   cancel` → `task.cancel()` raises `CancelledError` in the loop while **ffmpeg keeps
   running to its own timeout** (orphaned, pegging CPU/GPU). Same for the shared
   `jbrain.media` sampler `analyze_video` uses.
3. **A multi-minute blocking turn is the wrong shape.** `ASSISTANT.md` already says so:
   *"Long-running / expensive tools defer to the Postgres job queue: the tool enqueues a
   job, returns a handle inline, and the chat turn never blocks."*

This plan moves long media analysis onto the **job queue** and makes its subprocesses
**cancel-safe**, then **auto-resumes** the finished result into the chat. Peer to
`../reference/ASSISTANT.md` (the turn loop, guardrails, the job-defer rule, the
enact→outcome loop), `../reference/PROCESS.md`, and `../archive/STREAM_ANALYSIS_PLAN.md`
/ `../archive/VIDEO_ANALYSIS_PLAN.md` (what this builds on).

## Why it fits (the lean litmus)

Reuses the existing **Postgres job queue** (`jbrain.queue`, the worker's
`build_registry` actions), the `analyze_video_attachment` job as the template, the
**runs** row for the audit trail, the **SSE progress** channel, and the **enact→outcome
data-framed turn** (`ChatRequest.proposal_outcome`) the inline-approvals work already
uses to feed a server-authored result back into a chat. Net-new is one job def, a
run-scoped result handle (a URL has no attachment row to cache against), and a small
"kick + resume" tool shape. No new datastore, no new dependency.

## Owner decisions to lock (open)

- **Threshold — when in-turn vs. job.** `single` and a short `window` (≤ ~20–30 s,
  seconds to run) stay **in-turn** (their latency is the point). `full`, and a `window`
  whose audio would chunk (> `WHISPER_CHUNK_S`), **defer to a job**. Exact cutoff TBD.
- **Result delivery for a URL.** No attachment row to cache on. Options: (a) a small
  `media_analysis_results` run/session-scoped row keyed by a handle, reaped on a TTL;
  (b) deliver only via the resume turn's data frame (no persistence). Lean (a) so a
  reconnect/re-ask is free, mirroring the attachment cache.
- **Auto-resume trigger.** Job completion enqueues a **follow-up chat turn** carrying a
  server-authored outcome (the summary + the `video_analysis` view), framed as data not
  instruction (#1), exactly like `enact_outcome_summary`. The PWA shows an "analyzing…"
  placeholder that swaps to the card. Whether the resume is silent (card only) or the
  assistant comments is a UX call (GUI gate if it's a new surface).

## Waves

### Wave 1 — Cancel-safe subprocesses (independent, ship first)
Convert the ffmpeg/ffprobe legs to **`asyncio.create_subprocess_exec`** so a cancelled
turn terminates them promptly: on `CancelledError`, `.terminate()` (then `.kill()` after
a grace period), and enforce the existing time bounds with `asyncio.wait_for` instead of
`subprocess(timeout=)`. Touches `jbrain.stream` (`_extract_frames`/`_extract_audio`/
`_grab_one`) and the shared `jbrain.media` sampler (so `analyze_video` benefits too) —
the `VideoSampler` protocol goes async, rippling to `run_video_analysis`. **Valuable on
its own**, independent of the job move: it fixes the Stop-orphans-ffmpeg bug for every
media tool. Tests: a cancelled sample leaves no live child (assert the process is
reaped); bounds still enforced.

### Wave 2 — Job-backed long analysis
A worker job (generalize `analyze_video_attachment` into a media-analysis action, or a
sibling `analyze_media_job`) that runs resolve → sample → caption → transcribe (chunked)
→ reduce **off the interactive turn**, writing its result to the run-scoped handle
(owner decision above) and streaming progress to the run. The `analyze_stream` tool,
when the request crosses the threshold, **enqueues the job and returns inline
immediately** ("Analyzing the whole video — I'll follow up when it's ready"), with the
handle. Job cancellation (a real `cancel` on the job) replaces the un-killable in-turn
subprocess — Wave 1's async subprocesses run under the job's lifecycle. Tests: adapter/
queue fakes drive kick→job→result; a cancelled job stops the pipeline.

### Wave 3 — Auto-resume into the chat + threshold/UX
On job completion, deliver the result back into the originating chat as a **data-framed
follow-up turn** (reuse the `proposal_outcome` path): the PWA's "analyzing…" placeholder
swaps to the finished `video_analysis` card. Wire the **threshold** (Wave-2 decision) so
short grabs stay instant and long ones defer transparently — the model shouldn't have to
choose; the tool routes by estimated cost. Progress: the job's per-chunk / per-frame
status streams to the placeholder. Any new placeholder/badge is a GUI surface → the
3-mock gate (`PROCESS.md`).

## Out of scope (named)

- **Speeding up transcription.** One GPU; whisper already windows internally. Chunking
  (shipped) is reliability, not speed. The lever is the model (turbo) and the job move
  (it stops blocking the turn), not parallelism.
- **A general "background tool" framework.** This is the media case only, reusing the
  existing job queue — not a new async-tool abstraction.
- **Live-stream capture-over-time.** A live stream still samples a bounded window; this
  plan is about long *on-demand* analysis, not recording a live feed.

## Interim state (what shipped without this)

Until W1–W3 land, long `full`-mode analysis runs **in-turn**, protected by chunked
transcription (keeps the idle watchdog fed) and best-effort degradation, but a **Stop
during a long analysis leaves ffmpeg/whisper running until their own timeouts** (W1
closes this) and the turn blocks for minutes (W2 closes this). For a guaranteed
whole-video transcript today, the attachment → `analyze_video` job path already avoids
the in-turn limit.
