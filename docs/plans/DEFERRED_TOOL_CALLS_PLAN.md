# Deferred tool calls — turn-ending background jobs with a reusable status card

> **Status:** Scheduled · **Last verified:** 2026-07-18 · **Delivery:** one PR

A **reusable mechanism** for any long/expensive tool call to run **off the interactive
turn**: the tool kicks a background job, the **turn ends immediately**, a live
**status card** shows the work progressing, and when the job finishes its result
**auto-resumes into the chat**. `analyze_stream` (full-video) is the first adopter and
the reason this exists, but the point is the general primitive — future long tools
(bulk re-embed, a heavy image job, a wiki-restructure preview) plug into the same shape.

Motivated by three concrete problems shipping `analyze_stream`, all rooted in running a
minutes-long pipeline **inside the turn**:

1. **It fights the turn bounds.** A full-video pass survives only because the hard
   ceiling is generous (`_MAX_TURN_WALL_CLOCK_S = 3600`) and chunked transcription keeps
   the 10-min idle watchdog (`_TURN_IDLE_S = 600`) fed. Close to the edge, not inside it.
2. **Stop can't kill the work.** ffmpeg/whisper run via `subprocess.run` in
   `asyncio.to_thread`; a thread can't be cancelled, so `POST /chat/runs/{id}/cancel`
   leaves orphaned subprocesses running to their own timeouts.
3. **A multi-minute blocking turn is the wrong shape** — `ASSISTANT.md` already says
   long/expensive tools should *"defer to the Postgres job queue: the tool enqueues a
   job, returns a handle inline, and the chat turn never blocks."*

**Delivery: one PR** (owner decision — not the per-wave-PR default). The phases below are
build order within it, not separate PRs.

## Why it fits (the lean litmus)

Reuses the **Postgres job queue** (`jbrain.queue`, worker `build_registry` actions), the
**runs** row for progress + audit, the **tool-view registry** for the status card, and
the **data-framed follow-up turn** (`ChatRequest.proposal_outcome`) the inline-approvals
work already uses to feed a server-authored result back into a chat. Net-new is one
tool-result kind, one reusable view component, and a small run-scoped result row. No new
datastore, no new dependency — a general primitive expressed on existing parts.

## The mechanism (the reusable core)

1. **A `deferred` tool result.** A handler may return a new result kind meaning *"this is
   now running as job `{job_id}`; here is a `task_status` view."* The agent loop, seeing
   a deferred result, streams the status card and **ends the turn** (`end_turn`) — it does
   not block, and the guardrails/watchdog never apply to the background work.
2. **The job runs on the worker**, with **cancel-safe async subprocesses** (P1): a real
   `cancel` on the job `.terminate()`s ffmpeg/whisper promptly, replacing the un-killable
   in-turn subprocess. It streams progress (step/total + label) and writes a final result
   to a **run-scoped result row** (a URL has no attachment to cache against).
3. **A reusable `task_status` view component** renders the running process: a title, a
   progress bar + phase label, a state (running / done / failed / cancelled), and a
   **Stop**. It **live-updates** from the job's progress and, on completion, **swaps to
   the final result view** (for media, the existing `video_analysis` card). Data-only, no
   model-authored markup (#1/#9), like every tool view.
4. **Auto-resume.** On job completion the server authors the outcome and delivers it into
   the originating chat as a **data-framed follow-up turn** (the `proposal_outcome`
   path), so the assistant can react and the card lands even if the user navigated away.
5. **Cancellation is real.** Stop on the status card (or the chat) cancels the *job*,
   which terminates its subprocesses (P1) — no orphans.

## Phases (build order, one PR)

- **P1 — Cancel-safe async subprocesses.** Convert the ffmpeg/ffprobe legs in
  `jbrain.stream` and the shared `jbrain.media` sampler to `asyncio.create_subprocess_exec`
  with `asyncio.wait_for` bounds, terminating on cancel. Fixes the Stop-orphans bug for
  every media tool; the foundation the job's cancellation stands on.
- **P2 — The deferred-tool primitive.** The `deferred` tool-result kind + the agent-loop
  end-turn handling; a media-analysis worker job (generalizing `analyze_video_attachment`)
  that runs resolve→sample→caption→transcribe→reduce and writes the run-scoped result;
  the kick+handle path in `analyze_stream` for requests over the threshold.
- **P3 — The reusable `task_status` component + auto-resume.** The generic status card
  (**3-mock GUI gate**, `PROCESS.md`), its live progress channel, the swap-to-result on
  completion, and the completion→chat data-framed follow-up. Wire the in-turn-vs-job
  threshold so short grabs stay instant and long ones defer transparently (the model
  doesn't choose — the tool routes by estimated cost).

## Open decisions to lock

- **Threshold.** `single` / short `window` stay in-turn (latency is the point); `full`
  and a long `window` defer. Exact cutoff (duration? mode?) TBD.
- **Progress transport.** Live SSE from the job to the open card vs. the card polling the
  run status. Lean on the existing runs/SSE machinery.
- **Result persistence.** A run/session-scoped `media_analysis_results` row (reaped on a
  TTL) so a reconnect/re-ask is free, mirroring the attachment cache — vs. deliver-only.
- **Resume voice.** Silent (card only) vs. the assistant comments on completion.

## Out of scope (named)

- **Speeding up the work.** One GPU; chunking (shipped) is reliability, not speed. This
  plan changes *where* the work runs, not how fast.
- **A generic workflow/DAG engine.** This is single deferred tool calls on the existing
  job queue, not a new orchestration layer (the workflow engine already exists for
  scheduled pipelines).

## Interim state (pre-merge of this plan)

Long `full`-mode analysis runs **in-turn**, protected by chunked transcription and
best-effort degradation — but a **Stop leaves ffmpeg/whisper orphaned** (P1 closes this)
and the turn **blocks for minutes** (P2/P3 close this). For a guaranteed whole-video
transcript today, the attachment → `analyze_video` job path already avoids the in-turn
limit.
