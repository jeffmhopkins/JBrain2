# Watching YouTube channels into the video corpus

> **Status:** Living · **Last verified:** 2026-07-18

How to set up automatic nightly ingestion of a YouTube channel's new videos into the
external-source corpus, using a recurring **Jerv Task** (the shipped Tasks feature) — no
backend change, no deploy. Background: `../plans/EXTERNAL_VIDEO_INGESTION_PLAN.md`.

## What the tools give you

Three jerv tools make the corpus self-maintaining once a Task drives them:

- **`check_channel`** — lists a channel's recent uploads (optionally filtered by a title
  substring) and returns only the ones **not already in the corpus**.
- **`analyze_stream`** (full mode) — analyses a video and, on completion, **writes it through**
  to the corpus (summary + timeline passages + embeddings). Reuses the analysis it produces,
  so there is no extra cost, and a repeat is a no-op (`ON CONFLICT`).
- **`search_external`** — hybrid search over everything ingested, cited to the video + timestamp.

A Task is just a saved jerv prompt on a schedule; the agent loop does discovery → analysis.

## Create the Task

In the Tasks UI (or `POST /api/tasks`): persona **jerv**, a **repeat** schedule (e.g. daily at
02:00 in your timezone), and a prompt naming the channels + filters and a per-run cap. Example:

> For each of my watched channels below, call `check_channel`; for each NEW video it returns,
> call `analyze_stream` on the video URL in **full** mode to add it to my video library. Analyse
> at most **5** new videos this run — if there are more, they'll be caught next run. Don't
> re-analyse anything; `check_channel` already excludes what's in the library.
>
> Watched channels:
> - NASASpaceflight (`@NASASpaceflight`) — titles containing "Starship"
> - (add more as `channel-id-or-@handle` — optional title filter)

Notes:
- **Channel identifier:** a `UC…` channel id or an `@handle` (not a URL). `check_channel`
  validates this.
- **The cap N is the cost lever.** Each full analysis frame-captions the video (a serial run of
  vision calls); captions-first (#879) means no whisper pass for captioned videos, but frames
  still cost. Pick N so a run fits your nightly budget; the backlog drains over subsequent nights.
- **Live streams** are skipped automatically (full mode refuses a live stream); they're picked up
  once they're a finished VOD on a later run.
- **Watchlist config lives in the Task prompt** — edit the prompt to add/remove channels. (A
  first-class watchlist table + UI is a named follow-on if prompt-editing gets unwieldy.)

## Verifying it works

- Ad hoc first: in chat, ask jerv to analyse one video (full mode), then `search_external` for a
  phrase from it — it should come back cited to the timestamp.
- After a Task run: `search_external` finds the newly-ingested videos; the Tasks/Runs view shows
  the run and surfaces any failed analysis.

## Cost & retention

- Frame captioning dominates per-video cost (see the plan §10). Lower the frame density or drop to
  captions-only if you want cheaper, text-only coverage.
- The corpus grows nightly (chunks + 384-dim vectors + frame thumbnails). An external-source blob
  reaper + optional frame-JPEG pruning are named follow-ons (plan §13) if growth becomes a concern.
