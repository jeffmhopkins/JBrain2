# Watching YouTube channels into the video corpus

> **Status:** Living · **Last verified:** 2026-07-21

How to set up automatic nightly ingestion of a YouTube channel's new videos into the
external-source corpus, using a recurring **Jerv Task** (the shipped Tasks feature) — no
backend change, no deploy. Background: `../plans/EXTERNAL_VIDEO_INGESTION_PLAN.md`.

## What the tools give you

Three jerv tools make the corpus self-maintaining once a Task drives them:

- **`check_channel`** — lists a channel's recent uploads (title · length · publish date · a
  one-line description teaser) and returns only the ones **not already in the corpus**. jerv
  reads that metadata and **decides which are worth analysing** (e.g. news/update-style, not
  Shorts). Optional narrowing: `title_include` (keep titles containing ANY of several phrases)
  and `published_within_days` (a recency window like the last 7 days). The metadata comes from
  a cheap per-video resolve on the new uploads only — no download, far cheaper than an analysis.
- **`analyze_stream`** (full mode) — analyses a video and, on completion, **writes it through**
  to the corpus (summary + timeline passages + embeddings). Reuses the analysis it produces,
  so there is no extra cost, and a repeat is a no-op (`ON CONFLICT`).
- **`search_external_video`** — hybrid search over everything ingested, cited to the video + timestamp.

A Task is just a saved jerv prompt on a schedule; the agent loop does discovery → analysis.

> **Related — seeing a specific moment.** `analyze_stream` gives a text caption of a clip; to
> look at *one still* (e.g. "what module is on screen at 2:44?"), jerv uses **`grab_frame`** (a
> URL or attachment + a `seek`), which returns a reusable image it can then `analyze_image` or
> `compare_images` — the companion to the corpus tools (docs/plans/VIDEO_IMAGE_TOOLS_PLAN.md).
> A single-frame grab now honours `seek` (a fix for the `analyze_stream` single-mode bug that
> always sampled t=0 and returned a black intro frame).

## Create the Task

In the Tasks UI (or `POST /api/tasks`): persona **jerv**, a **repeat** schedule (e.g. daily at
02:00 in your timezone), and a prompt naming the channels + filters and a per-run cap. Example:

> For each of my watched channels below, call `check_channel` (pass `published_within_days: 7` so
> you only see the last week). It returns each new upload with its length, publish date, and a
> description teaser — use that to pick the ones that are clear **news or update-style** episodes
> and **skip** Shorts, clips, and off-topic uploads. For each you pick, call `analyze_stream` on
> the URL in **full** mode to add it to my library. Analyse at most **5** this run; the rest are
> caught next run. Don't re-analyse anything — `check_channel` already excludes what's in the library.
>
> Watched channels:
> - NASASpaceflight (`@NASASpaceflight`) — no title filter needed; judge from the metadata. (To
>   force a narrow instead, pass `title_include: ["Starship", "Starbase"]`.)
> - (add more as `channel-id-or-@handle`)

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

- Ad hoc first: in chat, ask jerv to analyse one video (full mode), then `search_external_video` for a
  phrase from it — it should come back cited to the timestamp.
- After a Task run: `search_external_video` finds the newly-ingested videos; the Tasks/Runs view shows
  the run and surfaces any failed analysis.

## Cost & retention

- Frame captioning dominates per-video cost (see the plan §10). Lower the frame density or drop to
  captions-only if you want cheaper, text-only coverage.
- The corpus grows nightly (chunks + 384-dim vectors + frame thumbnails). An external-source blob
  reaper + optional frame-JPEG pruning are named follow-ons (plan §13) if growth becomes a concern.
