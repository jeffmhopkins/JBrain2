# External Video Ingestion (YouTube corpus) — Build Plan

> **Status:** Proposed · **Last verified:** 2026-07-18

**A proposed build plan** (per `docs/DOC_LIFECYCLE.md`): shaped, not yet on the roadmap. It
builds on the shipped `analyze_stream` capability (URL resolution via yt-dlp, the shared
caption→fuse→reduce pipeline, and the **captions-first transcript** landed in #879) to turn
watched YouTube channels into a **durable, embedded, searchable corpus** the assistant can query.
Migration numbers below are placeholders (`00NN`); the source of truth is
`backend/migrations/versions/` — re-derive the head before building.

The design leads with the **trust boundary** (it determines the whole shape), then the storage
model, the poll/ingest pipeline, the search surface, scheduling, security, migrations, tests, and
waves.

---

## 1. Goal & scope

**Goal.** Watch a small set of YouTube channels/queries (e.g. NSF "This Week In Space"), and each
night ingest newly-published videos — transcript + summary + frame timeline + metadata + link — into
a **standalone external-source corpus** with embeddings, so the assistant (jerv) can search *what
was said and shown* across every ingested video, cited back to the original video and timestamp.

**In scope:**
- A new **isolated** storage model: `app.external_sources` (one row per video) + `app.external_source_chunks`
  (embedded, FTS-indexed transcript/timeline passages) — **general domain**, owner-scoped, RLS-firewalled.
- A **runtime-editable watchlist** (`app.external_watchlist`): per rule a `channel_id` + optional
  `title_include` filter, `enabled`, and a backfill policy — editable via API/Ops, no migration per rule.
- A **`poll_youtube` workflow action** (modeled on `triage_inbox`) that lists a channel's recent
  uploads via yt-dlp, dedups against the corpus, and enqueues analysis for each new **finished** VOD.
- An **`ingest_youtube_video` action** that runs the shared stream pipeline with `captions: auto`,
  persists the result into the corpus, and enqueues embedding — reusing `StreamAnalysisPipeline` and
  the `chunker`, leaving the interactive `analyze_stream` tool untouched.
- A **nightly, deadline-boxed schedule** (e.g. 02:00–04:00): don't *start* a new video past the
  window; in-flight finishes; backlog drains over subsequent nights.
- A dedicated **`search_external` agent tool**: hybrid (dense + FTS, RRF) search over the corpus,
  returning passages with the video URL + timestamp deep-link, cited like web results.
- The migrations + RLS isolation tests to the coverage gates; a phased rollout.

**Out of scope (named follow-ons):**
- **Feeding external content into the knowledge graph** (notes/entities/facts/wiki). External video is
  third-party, lower-trust content; it is deliberately *not* a source of truth (#7). A future
  **"promote to note"** action (owner-invoked, per passage) is the sanctioned bridge — not this phase.
- **Proactive surfacing** (a morning-brief "NSF posted X overnight" feed). Cheap to add on top since the
  summary already exists, but out of scope here — named follow-on.
- **Non-YouTube providers.** The pipeline already resolves other yt-dlp providers, and the schema is
  provider-agnostic (`provider` column), but only YouTube channel-listing + polling is built now.
- **Live-stream in-progress analysis.** Live is *detected* but deferred until a finished VOD exists.
- **Backfilling entire back-catalogs by default** (opt-in per rule; see §4.3).
- **A GUI corpus browser.** Search is via the agent tool this phase; an Ops/PWA corpus view is a
  follow-on (would trigger the `PROCESS.md` GUI gate).

**The trust frame (binding, not a footer).** The corpus answers *"what did this video say?"*, cited to
the source — never *"what is true?"*. Ingested claims never auto-promote into the wiki or the entity
graph, and the search tool's results are attributed to the third-party video, not asserted as owner facts.

---

## 2. What exists today (grounding)

Verified against the shipped code as of `Last verified`:

- **`analyze_stream`** (`agent/tools/analyze_stream.tool`, v4; handler `agent/streamtools.py`) resolves a
  YouTube URL with yt-dlp (`stream.py:resolve_stream`) and runs the shared **caption→fuse→reduce** core
  (`ingest/video.py`): sample+caption frames (vision), transcribe audio, fuse onto one `[mm:ss]` timeline,
  reduce to a summary. `full` mode spreads frames across the whole VOD.
- **Captions-first (#879, `jbrain.captions`)**: in `full` mode, `select_caption` picks the best track
  from yt-dlp's info dict (manual > ASR, preferred lang, word-level `json3` > vtt) and
  `fetch_caption_transcript` parses it into the **same `Transcript`/`Word` shape** whisper emits, over the
  SSRF-guarded egress. A `captions` preference (`auto`/`off`/`only`) selects the source; when captions win,
  the audio ffmpeg leg and whisper are skipped. Captions cover the **whole video with no ~30-min cap**
  (`MAX_FULL_AUDIO_S`, which bounds only the whisper fallback).
- **The deferred path** (`ingest/stream_analysis.py:StreamAnalysisPipeline`, worker job `analyze_stream_url`)
  persists to **`app.media_analysis_results`** (migration 0132) — but that table is **owner-only,
  session-scoped, transient** (reaped by `run_id` CASCADE or session TTL). It is chat output, **not** a
  durable corpus. This plan adds the durable, indexed store the branch name promises.
- **Metadata available** from `ResolvedStream` (`stream.py`): `video_id`, `title`, `webpage_url`, `provider`,
  `duration_s`, `is_live`. **Not yet extracted** (present in yt-dlp's info dict, dropped today):
  `channel`/`uploader`, `upload_date`, `description`. §4.1 extends `ResolvedStream` to keep them.
- **Embeddings** (`embed.py`): local TEI `bge-small-en-v1.5`, **384 dims**, `vector(384)` + HNSW cosine,
  written by a follow-up job via `EmbedClient.embed` + `cast(:emb AS vector)` (never the ORM). Hybrid search
  = dense + FTS legs fused with **RRF** (`search/service.py`, `search/repo.py`). Model-change re-embed is
  `analysis/reembed.py` (`_TARGETS`).
- **Chunking** (`ingest/chunker.py`): pure `chunk_text(source)` → paragraph + section chunks with exact
  offsets. Reused here over the fused-timeline text.
- **Workflow engine** (`workflow/`): `poll_youtube`/`ingest_youtube_video` are **actions** naming worker
  handlers (registry bijection enforced at boot). Schedules are **interval + `next_run_at`** rows (no cron),
  ticked every 30s (`scheduler.py`), seeded by a migration (pattern: `0038`, `0096`). Closest precedent for
  a scheduled external-API poll: the Gmail **`triage_inbox`** sweep (`worker.py`, `gmail/`, `connectors/`).
- **Agent tools** (`agent/`): `.tool` sidecar + `build_*_handlers` factory wired into `build_registry`
  (`readtools.py`), RLS-scoped via `ToolContext.session`, results returned as `ToolOutput` with citation
  cards (`WebSource`/`NoteSource`). The `web_search` tool (`webtools.py`, SearXNG) is the citation model to mirror.

---

## 3. Storage model — the isolated corpus (with DDL)

Two new tables, **completely parallel to `notes`/`chunks`** but never joined into the graph. Both carry
`domain_code text NOT NULL DEFAULT 'general' REFERENCES app.domains(code)` and the standard RLS quartet.
The corpus is **owner + general-domain**; nothing here is health/finance/location.

> **Why a parallel `external_source_chunks` and not `app.chunks`?** `app.chunks.note_id` is `NOT NULL`
> (FK → `app.notes`). An external video is not a note and must never mint one (that is the trust boundary,
> §1). A parallel chunk table keeps the isolation structural — the graph's search legs (`chunks`, `wiki_*`)
> physically cannot surface external passages, and `search_external` physically cannot surface owner notes.

### 3.1 `app.external_sources` — migration `00NN` (one row per video)

Doubles as the **dedup ledger and state machine**: a row is created at *discovery* (metadata only,
`status='pending'`) and filled in on *analysis*, so `video_id` uniqueness prevents double-ingest across
overlapping watchlist rules.

```sql
CREATE TABLE app.external_sources (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider         text NOT NULL DEFAULT 'youtube',       -- yt-dlp extractor name
    video_id         text NOT NULL,                         -- provider's own id (YouTube video id)
    url              text NOT NULL,                         -- canonical webpage_url
    title            text,
    channel_id       text,                                  -- yt-dlp channel_id (stable)
    channel_name     text,
    published_at     timestamptz,                           -- from upload_date (day precision; NULL if absent)
    duration_s       integer,
    summary          text,                                  -- the reduce-step summary (NULL until analyzed)
    summary_embedding vector(384),                          -- source-level dense vector (unmapped in ORM)
    embedding_model  text,
    transcript_source text,                                 -- 'captions:manual' | 'captions:auto' | 'whisper' | '' (§4.2)
    analysis         jsonb,                                 -- {duration_ms, frames:[{t_ms,caption,thumb_id}], transcript:{...}}
    tool             text,                                  -- pipeline provenance (router spec string)
    status           text NOT NULL DEFAULT 'pending'        -- pending|pending_vod|analyzing|done|unavailable
        CHECK (status IN ('pending','pending_vod','analyzing','done','unavailable')),
    attempts         integer NOT NULL DEFAULT 0,            -- for the dead-letter cap (§4.4)
    last_error       text,
    discovered_by    uuid REFERENCES app.external_watchlist(id) ON DELETE SET NULL,  -- which rule first found it
    discovered_at    timestamptz NOT NULL DEFAULT now(),
    analyzed_at      timestamptz,
    domain_code      text NOT NULL DEFAULT 'general' REFERENCES app.domains(code),
    UNIQUE (provider, video_id)                             -- the dedup key
);
CREATE INDEX external_sources_status_idx  ON app.external_sources (status, discovered_at);
CREATE INDEX external_sources_channel_idx ON app.external_sources (channel_id, published_at DESC);
CREATE INDEX external_sources_summary_embedding_idx
    ON app.external_sources USING hnsw (summary_embedding vector_cosine_ops);

ALTER TABLE app.external_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.external_sources FORCE  ROW LEVEL SECURITY;
CREATE POLICY external_sources_domain ON app.external_sources
    USING (app.has_domain_scope(domain_code)) WITH CHECK (app.has_domain_scope(domain_code));
GRANT SELECT, INSERT, UPDATE, DELETE ON app.external_sources TO jbrain_app;
```

Frames' `thumb_id`s are content-addressed blobs written through the storage abstraction (#2), exactly as the
attachment-video path does today — kept so a search hit can show a thumbnail at its timestamp.

### 3.2 `app.external_source_chunks` — migration `00NN` (embedded, FTS-indexed passages)

Mirrors `app.chunks` (the dense + FTS RRF surface), minus the note FK:

```sql
CREATE TABLE app.external_source_chunks (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id     uuid NOT NULL REFERENCES app.external_sources(id) ON DELETE CASCADE,
    seq           int  NOT NULL,
    t_ms          int,                                      -- timeline offset for the deep-link (NULL for summary chunk)
    text          text NOT NULL,
    tsv           tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    embedding     vector(384),                              -- unmapped in ORM; written via cast(:emb AS vector)
    embedding_model text,
    domain_code   text NOT NULL DEFAULT 'general' REFERENCES app.domains(code),
    UNIQUE (source_id, seq)
);
CREATE INDEX external_source_chunks_tsv_idx       ON app.external_source_chunks USING GIN (tsv);
CREATE INDEX external_source_chunks_embedding_idx ON app.external_source_chunks USING hnsw (embedding vector_cosine_ops);

ALTER TABLE app.external_source_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.external_source_chunks FORCE  ROW LEVEL SECURITY;
CREATE POLICY external_source_chunks_domain ON app.external_source_chunks
    USING (app.has_domain_scope(domain_code)) WITH CHECK (app.has_domain_scope(domain_code));
GRANT SELECT, INSERT, UPDATE, DELETE ON app.external_source_chunks TO jbrain_app;
```

Chunk text is drawn from the **fused timeline** (interleaved frame captions + speech), the richest
searchable surface, plus one leading chunk for the summary (`t_ms` NULL). Re-analysis is idempotent: delete
the source's chunks and rebuild, then re-enqueue embedding (the `ingest_note`→`embed_note` pattern).

### 3.3 `app.external_watchlist` — migration `00NN` (runtime-editable rules)

```sql
CREATE TABLE app.external_watchlist (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider       text NOT NULL DEFAULT 'youtube',
    channel_id     text NOT NULL,                           -- yt-dlp channel_id (stable), NOT the handle
    channel_label  text,                                    -- human label for the Ops UI
    title_include  text,                                    -- optional case-insensitive substring; NULL = whole channel
    enabled        boolean NOT NULL DEFAULT true,
    backfill_since  timestamptz,                            -- opt-in: ingest matching videos published on/after this; NULL = forward-only
    last_checked_at timestamptz,
    domain_code    text NOT NULL DEFAULT 'general' REFERENCES app.domains(code),
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz
);
-- standard RLS quartet (ENABLE+FORCE, has_domain_scope USING/WITH CHECK, grants)
```

Editable via a thin owner API (`POST/PATCH/DELETE /api/external/watchlist`) and surfaced in Ops. No
migration per rule. `title_include` is a substring by default (regex is a named follow-on if power is needed).

---

## 4. Poll / ingest pipeline

Two worker actions, both registered in `build_registry` (`worker.py`) with handlers in `impls`; the schedule
is a seed migration. All work runs under `queue.SYSTEM_CTX` (cross-domain system context), writing
general-domain rows.

### 4.1 Metadata extraction (`ResolvedStream` extension)

Extend `ResolvedStream` (`stream.py`) + `_select_media` to keep `channel_id`, `channel_name`, `upload_date`,
`description` from yt-dlp's info dict (already fetched at resolve time — no extra cost). Purely additive;
existing `analyze_stream` callers ignore the new fields. `published_at` parses `upload_date` (YYYYMMDD → day
precision, NULL when absent).

### 4.2 `ingest_youtube_video` action — analyze + persist + embed

Handler wraps `StreamAnalysisPipeline` and runs the shared `full`-mode pipeline with **`captions: auto`**
(captions-first, whisper fallback for the rare uncaptioned upload). On completion it, in one RLS-scoped
transaction:
1. Upserts the `external_sources` row (`status='done'`, summary, `analysis`, `transcript_source` reported by
   the pipeline, `channel_*`, `published_at`, `duration_s`, `analyzed_at`, `tool`).
2. Deletes + rebuilds `external_source_chunks` from the fused timeline (via `chunker.chunk_text`) + a summary
   chunk.
3. Enqueues **`embed_external_source`** (a new follow-up embed job) to fill `summary_embedding` and each
   chunk `embedding` via `EmbedClient` + `cast(:emb AS vector)`, mirroring `NoteEmbedder.embed_note`.

The interactive `analyze_stream` tool is **untouched** — it still returns an ephemeral card. This action is a
separate, persisting path. `captions: only` is an alternative (§11 open decision) if we want to guarantee
zero whisper spend in the batch at the cost of skipping uncaptioned videos.

### 4.3 `poll_youtube` action — discover new videos

Per enabled watchlist row: list the channel's recent uploads via yt-dlp's flat playlist extraction
(`extract_flat`, cheap — ids + titles, no per-video resolve), then:
- Filter by `title_include` (case-insensitive substring) when set.
- Filter by recency: **forward-only** (published after `last_checked_at`, or after row creation on first run)
  unless `backfill_since` is set, in which case include matching videos published on/after it.
- Skip any `video_id` already present in `external_sources` (the dedup ledger).
- For each survivor, insert a `pending` `external_sources` row and enqueue `ingest_youtube_video` (subject to
  the window gate, §5). If the listing marks it live/upcoming, insert `pending_vod` and **do not** enqueue —
  the next poll re-checks and promotes it once a finished VOD exists.
- Stamp `last_checked_at`.

Discovery is bounded (list only the first N of the uploads feed) so a channel with a huge history can't
enqueue thousands of jobs in one tick; the rest is reached only via an explicit `backfill_since`.

### 4.4 Failure handling (dead-letter)

A video that fails resolution/analysis (private, members-only, age-gated, geo-blocked, removed) increments
`attempts` and records `last_error`. After a cap (e.g. 3), the row is marked `status='unavailable'` and never
retried — one bad video can't wedge the nightly batch or retry forever. Transient errors ride the engine's
existing job-retry semantics below the cap.

---

## 5. Scheduling — nightly, deadline-boxed

A seed migration (pattern: `0038`/`0096`) inserts a `pipelines` row (`poll_youtube` step), a `schedules` row
(interval `86400`, `next_run_at` seeded to the next 02:00 in the configured tz), and a `manual=true`
`triggers` row (Ops-fireable). The engine has **no cron**; a fixed daily interval + seeded `next_run_at` gives
"02:00 nightly."

**The window gate (the "max 2 hours" tooth).** When the 02:00 trigger fires, `poll_youtube` computes a window
deadline (`start + WINDOW_SECONDS`, e.g. 2h → 04:00) and writes it to a settings row
(`youtube_window_until`). Both actions honor it: `ingest_youtube_video` checks `now < youtube_window_until`
**before starting** the expensive analysis; past the deadline it leaves the row `pending` and returns (drains
next night). An **in-flight** analysis is never killed — the gate blocks *starts*, not running jobs. Because
`captions: auto` collapses most videos to caption-fetch + frame captioning (no whisper), the window goes far.

Backlog is durable in `app.jobs` + the `pending` rows, so a channel that dumps many uploads drains over
successive nights automatically. Ops "run now" fires the same trigger off-schedule.

---

## 6. Search surface — `search_external` tool

A dedicated agent tool (not folded into the graph `search`, to keep trust tiers distinct):

- **Sidecar** `agent/tools/search_external.tool`: `permission: read`, `domains: [general]`, params
  `{query (required), limit (default 6, max 10)}`, a prose description scoping it to the third-party video
  corpus (explicitly: results are *what a video said*, cited, not owner facts).
- **Handler** `build_external_handlers(maker)` → `{"search_external": handler}`, wired into `build_registry`.
  It runs a hybrid query mirroring `SearchService`: embed the query via `EmbedClient`; a dense leg over
  `external_source_chunks.embedding` (`<=>` cosine) + summary-level dense over `external_sources.summary_embedding`;
  an FTS leg via `websearch_to_tsquery` over `chunks.tsv`; fuse with **RRF** (reuse the existing `_fuse`
  helper or its logic). Degrades to FTS-only if the embed container is down, matching `SearchService`.
- **Result**: a `ToolOutput` whose text lists each hit as `title — channel — passage` with a **timestamped
  deep-link** (`{url}&t={t_ms//1000}s`), and `web_sources` citation chips (reusing `WebSource(url, title)`) so
  jerv cites video passages with `[^n]` footnotes exactly as it cites web results. All queries run inside
  `ctx.session` (RLS), so the general-domain policy applies.
- **Persona wiring** (`agents.py`): available to `curator` (default, `tools=None` auto-includes it for
  general scope) and added to `JERV_TOOLS` so the sandboxed web persona can search the curated corpus
  alongside `web_search`. The model picks web vs. curated-corpus per query.

---

## 7. Security & RLS

- Every new table is **general-domain, owner-scoped**, with `ENABLE`+`FORCE ROW LEVEL SECURITY`, the shipped
  `has_domain_scope(domain_code)` policy (USING + WITH CHECK), and `jbrain_app` grants incl. DELETE (the
  ingest path re-derives chunks; watchlist rows are editable). Each ships an **RLS isolation test** modeled on
  `test_domain_scope_firewall_pattern` (`tests/integration/test_rls.py`): a general-scoped session sees rows,
  an UNSCOPED session sees none, a health-only token sees none, an owner sees all, and a cross-domain INSERT
  is rejected by WITH CHECK.
- **Egress discipline.** yt-dlp channel-listing and caption fetches are outbound legs; they carry the same
  SSRF-guarded, redirect-refusing, size-capped discipline the media URL and #879's caption fetch already use
  (`web/fetch.py:guard_public_host`). Only provider URLs (from yt-dlp's own info dict) are fetched — never
  model-supplied URLs.
- **Trust isolation is structural** (§3): external chunks live in their own table with their own search legs,
  so external passages can never rank in the graph `search` and owner notes can never rank in `search_external`.
- **No new secrets required** if we use yt-dlp's keyless channel listing. If a YouTube Data API key is later
  preferred for listing, it plugs into the `connectors`/settings store like Gmail's credentials — never
  hardcoded, never model-supplied.

---

## 8. Migrations (snapshot; re-derive the head)

1. `00NN_external_sources` — the source table + summary HNSW index + RLS quartet.
2. `00NN_external_source_chunks` — the chunk table + tsv GIN + embedding HNSW + RLS quartet.
3. `00NN_external_watchlist` — the watchlist table + RLS quartet.
4. `00NN_seed_youtube_poll` — the `pipelines`/`schedules`/`triggers` rows for the 02:00 nightly poll.

**Non-migration code changes:** extend `ResolvedStream` (§4.1); `MAX_FULL_AUDIO_S` raised to `90 * 60`
(whisper *fallback* ceiling only — captioned videos are already uncapped); add the two actions +
`embed_external_source` handler to `build_registry`/`impls`; add `external_sources`/`external_source_chunks`
to `reembed.py`'s `_TARGETS` for model-change re-embedding; the `search_external` tool + handler; the
watchlist API.

---

## 9. Tests (to the coverage gates: 80% backend, security paths 100%)

- **Unit (LLM/embed/network faked):** watchlist filtering (title substring, forward-only vs backfill,
  dedup-skip); `poll_youtube` live/upcoming → `pending_vod` (no enqueue); the window gate (start blocked past
  deadline, in-flight unaffected); dead-letter after N attempts; `ResolvedStream` metadata extraction;
  chunk-building from a fused timeline; RRF fusion of external legs; `search_external` result formatting +
  timestamp deep-link + degraded (embed-down) FTS-only path.
- **Integration (real Postgres via testcontainers):** the three RLS isolation tests (§7); ingest→persist→embed
  round-trip writing real chunks + vectors; `search_external` returning a seeded passage under a general scope
  and **nothing** under an UNSCOPED/health-only scope; idempotent re-ingest (delete+rebuild chunks, no dupes);
  the graph `search` never returning an external chunk and `search_external` never returning a note
  (structural-isolation proof).
- **Digest pins:** `search_external.tool` and any `.prompt` change bump their version/digest.

---

## 10. Waves

- **W1 — Storage bedrock.** The three tables + migrations + RLS isolation tests; `ResolvedStream` metadata
  extension; `MAX_FULL_AUDIO_S` bump. (No behavior yet; the firewall proven first.)
- **W2 — Ingest pipeline.** `ingest_youtube_video` + `embed_external_source` + `reembed.py` targets; the
  analyze→persist→embed round-trip and its tests. Manually ingestible end to end.
- **W3 — Poll + schedule.** `poll_youtube`, the watchlist API, the seed migration, the window gate, dead-letter;
  their unit tests. The nightly loop runs.
- **W4 — Search tool.** `search_external` sidecar + handler + persona wiring; formatting + degraded-path +
  isolation tests. Jerv can query the corpus.

Each wave: independent adversarial review (reviewer ≠ builder) per `PROCESS.md`, local lint+typecheck+unit
green before merge, one PR per wave, CI green before proceeding. No GUI gate this phase (search is via the
tool; an Ops watchlist view, if built, triggers it).

---

## 11. Open decisions (for the owner)

1. **`captions: auto` vs `captions: only` in the batch.** `auto` = whisper fallback for uncaptioned videos
   (fuller coverage, occasional GPU spend); `only` = captions or no transcript (strictly predictable window,
   some videos get frames+summary but no speech). Recommend `auto`.
2. **Frame retention.** Keep per-video frame JPEGs (thumbnails at timestamps, nicer search UX, cheap storage)
   or drop them and keep text+timeline only. Recommend keep.
3. **Window length + start.** 02:00–04:00 (2h) assumed; confirm the wall-clock window and timezone.
4. **`title_include` semantics.** Substring (simple) now; regex is a follow-on. Confirm substring suffices.
5. **Discovery depth.** How many of a channel's most-recent uploads each poll inspects (bounds first-run cost).

---

## 12. Reconciliation on promotion (per `DOC_LIFECYCLE.md`)

When picked up: reconcile against `CLAUDE.md` non-negotiables (LLM adapter, storage abstraction, RLS +
isolation tests, docs-with-code), add a `ROADMAP.md` slot + a `plans/README.md` row, flip to `Scheduled`, and
`git mv` from `proposed/` to `plans/`. On the last wave, flip to `Shipped` and archive, carrying any residual
(the promote-to-note bridge, proactive surfacing, non-YouTube providers) into `ROADMAP.md`.
