# Photo Archive Pipeline — Design Spec

> **Status:** Proposed (icebox) · **Last verified:** 2026-07-03

> **Status: proposed, not scheduled.** This is a forward-looking design dropped
> in for the record; nothing here is built and it is not on the current roadmap
> (the active frontier is Phase 6, the wiki). When/if it is picked up, it must be
> reconciled with the root `CLAUDE.md` non-negotiables — in particular: all LLM
> (and VLM) calls go through the LLM adapter, all file I/O goes through the
> storage abstraction, and the new tables (`assets`, `asset_paths`, `faces`,
> `people`, `albums`) need RLS scoping + isolation tests, since photo content is
> as sensitive as health/finance/location. The shared-Postgres + on-box model
> assumptions here align with `STRIX_HALO_SETUP.md`.

A self-hosted system to ingest a decade of unprocessed phone dumps, dedupe and
enrich them, recognize people, and make everything searchable through a browser
viewer. Built as an agentic toolset on top of an existing JBrain2 instance
(notes → RAG → LLM-maintained wiki, Ubuntu + Docker, `gpt-oss-120b` as the main LLM).

---

## 1. Core idea

The pipeline is a **staged, idempotent map over files**, not an agent loop over
images. Cheap deterministic work runs on every file; medium-cost vision work runs
on every file; the expensive 120B model runs **only on the residual** — the items
that still lack a date or identity after the cheap passes.

The single most important architectural fact: **`gpt-oss-120b` is text-only.** It
cannot see images. Every image must be turned into *text* (caption, OCR, class label)
by a separate vision worker before the 120B ever reasons about it.

The spine of the system is a **content-hash-keyed table**. The `sha256` of each file
is the primary key, which makes dedup fall out for free and lets one logical asset
map to many physical file locations.

---

## 2. Technology stack

| Layer | Choice | Notes |
|---|---|---|
| Host | Strix Halo (Ryzen AI Max+ 395), 128GB unified memory, iGPU `gfx1151` | Ubuntu + Docker |
| Orchestration / agent | JBrain2 + `gpt-oss-120b` (text-only) | Conductor + residual reasoning only |
| Vision worker | Small VLM (e.g. Qwen2.5-VL-7B class) via Ollama / llama.cpp | image → caption / OCR / class label |
| Face recognition | InsightFace (`buffalo_l`), 512-d embeddings | direct, into our own table |
| Image/text embeddings | CLIP (image + text encoders) | subject search + similarity |
| Database | PostgreSQL + `pgvector` | **shared with JBrain2's RAG store** |
| Dedup | `sha256` content hash (exact) + CLIP similarity (near-dupe) | |
| Metadata | `exiftool` | EXIF dates, GPS, filename-date backfill |
| Backend | Python | matches JBrain2 |
| Frontend | TypeScript (browser) | matches JBrain2 |

### GPU note (Strix Halo)

- ROCm 7.2.x auto-detects Strix Halo for the LLM/VLM stack (Ollama, llama.cpp).
- Immich's bundled ROCm image is still catching up to `gfx1151`; not relevant if
  faces run via InsightFace directly.
- For the **one-time backfill**, CPU inference is an acceptable, reliable fallback —
  it's an overnight batch. Pursue GPU (`HSA_OVERRIDE_GFX_VERSION=11.5.1`) only if
  re-running ML often.

---

## 3. Data model

Lives in the same Postgres + pgvector as JBrain2's RAG.

```sql
-- One logical asset per unique file content
assets (
  id                      bigserial primary key,
  sha256                  text unique not null,
  mime                    text,
  bytes                   bigint,
  width                   int,
  height                  int,
  captured_at             timestamptz,
  captured_at_source      text,        -- exif | filename | inferred | unknown
  captured_at_confidence  real,        -- 0..1, meaningful for 'inferred'
  captured_at_rationale   text,        -- why, when inferred (audit trail)
  category                text,        -- photo | screenshot | meme | document | receipt
  category_confidence     real,
  caption                 text,        -- VLM-generated
  ocr_text                text,        -- VLM/OCR-extracted
  image_emb               vector(768), -- CLIP image embedding
  status                  jsonb,       -- which stages are done (see §5)
  created_at              timestamptz default now()
)

-- One hash → many physical locations (the dedup ledger + hash→location map)
asset_paths (
  asset_id  bigint references assets(id),
  path      text,
  present   boolean default true       -- re-validated on re-ingest
)

-- One row per detected face
faces (
  id         bigserial primary key,
  asset_id   bigint references assets(id),
  bbox       int[],                     -- [x, y, w, h]
  emb        vector(512),               -- InsightFace embedding
  person_id  bigint references people(id),  -- null until named/matched
  det_score  real
)

people (
  id    bigserial primary key,
  name  text
)

-- Optional, for proposed groupings
albums (
  id     bigserial primary key,
  title  text,
  rule   jsonb        -- e.g. date range / person / event criteria
)
album_assets ( album_id bigint, asset_id bigint )
```

**Why hash-keyed:** on ingest, hash the file; if the hash already exists, just append
an `asset_paths` row — never reprocess. Exact duplicates collapse automatically.
Near-duplicates (re-compressed messenger copies) escape hashing and are caught later
by CLIP similarity as a review queue.

---

## 4. Process / pipeline

Each stage operates on *"assets where this stage isn't done yet"* (read from `status`),
returns counts/IDs (never row blobs), and is safely re-runnable.

```
2TB inbox (read-only)
   │  ingest_inbox()      — hash, dedup, write assets + paths
   ▼
[CHEAP · all files]       — deterministic, no model
   extract_exif()         — date, GPS, camera, dimensions
   date_from_filename()   — IMG_2015…, Screenshot_…, WhatsApp regex
   ▼
[MEDIUM · all files]      — vision worker (VLM) + embedding + faces
   classify()             — photo/screenshot/meme/document/receipt
   caption() / ocr()      — text for search + reasoning
   embed_image()          — CLIP vector
   detect_faces()         — bbox + 512-d embedding per face
   ▼
[COSTLY · residual only]  — 120B + RAG; only undated/unknown items
   infer_date()           — caption+OCR+GPS+people → RAG notes → date range
   infer_identity()       — unknown face + co-occurrence + wiki → name guess
   ▼
Assets DB (pgvector)  ──►  search tools  ──►  browser viewer
```

The cost gradient is the whole point: deterministic on everything (instant), VLM on
everything (overnight batch), 120B on a few thousand hard cases (where it actually
earns its keep by cross-referencing your own notes to recover dates/identities a pure
pixel pipeline never could).

---

## 5. Agent tool catalog

Keep tools **granular** — never a monolithic `process_photos()`. Granularity is what
lets the 120B make real decisions ("8k are unclassified but already dated → run only
`classify` on those").

**Ingest & orchestration**

- `ingest_inbox(path) -> {new, duplicate, total}` — hash, dedup, insert.
- `pipeline_status() -> {stage: count}` — the agent's situational awareness; drives resume.

**Deterministic metadata** (all files)

- `extract_exif(asset_ids) -> {updated}` — sets `captured_at` + source=`exif`.
- `date_from_filename(asset_ids) -> {updated}` — sets source=`filename`.

**Vision worker — VLM, image → text** (all files)

- `classify(asset_ids) -> {asset_id: {category, confidence}}`
- `caption(asset_ids) -> {asset_id: caption}`
- `ocr(asset_ids) -> {asset_id: text}`

**Embeddings**

- `embed_image(asset_ids) -> {embedded}` — CLIP into `image_emb`.

**Faces** (InsightFace)

- `detect_faces(asset_ids) -> {faces_found}`
- `cluster_faces() -> {cluster_id: face_ids[]}`
- `name_person(cluster_or_face_ids, name) -> person_id` — the "Jeff" step.
- `match_faces(asset_ids) -> {matched}` — cosine vs known people.

**Agent reasoning — 120B + RAG, residual only**

- `infer_date(asset_id) -> {range, confidence, rationale}` — writes source=`inferred`, never overwrites a real EXIF date.
- `infer_identity(face_id) -> {name_guess, confidence, rationale}`
- `propose_albums() -> [album]` — group by trip/event from dates+captions+faces.

**Search — also the viewer's backend**

- `search_text(query) -> [asset]` — CLIP text encoder → cosine over `image_emb`.
- `search_person(name) -> [asset]`
- `search_similar(asset_id) -> [asset]` — image→image; doubles as near-dup review.
- `get_assets(filters, page) -> [asset]` / `thumbnail(asset_id) -> url`

---

## 6. Integration with JBrain2

JBrain2 is the **conductor and the brain**, not the workhorse.

- **Tools register in JBrain2's agentic tool framework.** Tool *execution* is plain
  Python doing a `map`; the 120B only orchestrates by batch and reads summaries.
- **Shared Postgres + pgvector.** Image embeddings live in the same store as the RAG
  index — one query path, no second vector DB.
- **The unique unlock = RAG over your own notes.** `infer_date` / `infer_identity`
  feed the VLM's text output into RAG against your wiki ("we moved to the blue house
  in 2019") to recover metadata that's gone from the file. This is the one thing a
  standalone pipeline can't do, and it's exactly what JBrain2 is shaped for.
- **Vision worker runs alongside** as a separate model the tools call — it bridges
  pixels to the text-only 120B.
- **Immich is optional.** Faces via InsightFace-direct fit the custom store better.
  Keep Immich only if you want its mobile auto-backup for *future* photos (a separate,
  legitimate reason) — it is not needed for the archive pipeline.

---

## 7. Design principles (non-negotiables)

1. **Staged + idempotent.** Every tool reads `status`, acts on the undone set, is
   re-runnable. Crash at 30k files → resume without redoing 30k.
2. **Tools return IDs and counts, never blobs.** The 120B never receives 40k rows.
3. **Cost gradient.** Cheap → medium → costly, each running on a smaller set.
4. **Inferred ≠ known.** `captured_at_source` is load-bearing. A guess is a flagged
   hypothesis with confidence + rationale; it never overwrites a real date.
5. **Immutable inbox.** The 2TB SSD is strictly read-only landing. Derived data goes
   to the DB + a separate library path. A bad run can never corrupt originals;
   re-ingest is always safe.

---

## 8. Proposed UI (browser viewer)

A thin TypeScript frontend over the search tools. Core views:

- **Timeline** — chronological grid. **Inferred dates rendered distinctly** (e.g.
  dashed/colored) so a guess never looks like ground truth. Filter by year/month.
- **People** — face-cluster gallery; click a person → all their photos. Surfaces
  unnamed clusters for the naming workflow.
- **Subject / semantic search** — free-text box → CLIP text→image ("beach sunset 2016").
- **Similar** — "find more like this" from any asset (image→image embedding).
- **Asset detail** — original + all `asset_paths`, faces (named/unnamed), caption,
  OCR text, full metadata, and the **inferred-date rationale** when applicable.

Review/maintenance queues (these are where the human + agent collaborate):

- **Face naming queue** — confirm/merge proposed clusters; one-click `name_person`.
- **Duplicate review queue** — near-dupes from `search_similar` above a threshold;
  pick the keeper.
- **Low-confidence queue** — inferred dates/identities below a confidence bar for
  human confirmation.

UI principle: the viewer reads the DB directly for browsing; it calls the agent only
for things that benefit from reasoning (ambiguous identity, inferred dating, album
proposals). Browsing must never block on the 120B.

---

## 9. Open decisions

- **Faces: InsightFace-direct vs Immich.** Leaning InsightFace-direct (one store, one
  query path, no second Docker stack). Revisit only if mobile auto-backup is wanted.
- **VLM choice + batch throughput target.** Pick a 7B-class VLM; optimize for
  batch throughput, not latency. Benchmark on real files before committing.
- **GPU vs CPU for backfill.** Start CPU (reliable overnight batch); add ROCm later
  if ML re-runs often.
- **Library layout.** Where do "kept" originals live vs the immutable inbox? Decide the
  canonical library path and whether files are copied or referenced in place.

---

## 10. Suggested build order

1. Schema + `ingest_inbox` (hash, dedup, paths). Prove dedup on a real dump subset.
2. Deterministic metadata (`extract_exif`, `date_from_filename`). Get the timeline right.
3. Vision worker + `classify` → junk separation (real photos vs screenshots/memes).
4. `embed_image` + `search_text` / `search_similar`. First useful search.
5. Faces: `detect_faces` → `cluster_faces` → `name_person` → `match_faces`.
6. Residual reasoning: `infer_date` / `infer_identity` over JBrain2 RAG.
7. Browser viewer (timeline, people, search) + review queues.
8. Optional: `propose_albums`, Immich for future-photo mobile backup.
