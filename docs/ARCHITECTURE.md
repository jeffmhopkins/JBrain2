# JBrain2 — Architecture

A personal knowledge system: notes go in, a RAG pipeline indexes them, and an
LLM maintains a wiki built **exclusively from notes as primary sources**. Over
time it extends to a personal agent, structured records (lists, labs,
appointments), guided-intake share links, and Life360-style location tracking.

## System shape

One Docker Compose stack on an Ubuntu host, reachable on a public domain.

| Container | Technology | Role |
|---|---|---|
| `proxy` | Caddy | Auto-TLS, serves the built PWA, routes `/api` |
| `api` | Python / FastAPI | REST API, auth, CRUD, search, chat |
| `worker` | Same image as `api` | Job-queue consumer: extraction, chunking, embedding, analysis, wiki builds |
| `db` | TimescaleDB-HA (Postgres + Timescale + PostGIS; pgvector) | The single stateful service (see below) |
| `embed` | HF text-embeddings-inference (CPU) | Local embedding model behind HTTP; GPU-swappable later |

Attachments are content-addressed blobs (sha256) on a disk volume behind a
storage abstraction, so S3/MinIO can replace the filesystem without touching
callers.

### One database, six jobs

Postgres deliberately does everything stateful: relational store, vector store
(pgvector HNSW), full-text search (`tsvector`), time-series (Timescale
hypertables for GPS fixes), spatial queries (PostGIS for geofences), job queue
(`SELECT ... FOR UPDATE SKIP LOCKED`), and workflow state. MVCC means readers
never block writers. No Redis, no broker, no separate vector DB — a personal
system stays operable by one person.

## Backend

- **FastAPI + Pydantic** (async throughout), **SQLAlchemy 2 + Alembic**.
- Job queue on Postgres (Procrastinate or equivalent SKIP-LOCKED pattern) with
  a scheduler for nightly work.
- Document parsing: PyMuPDF for PDFs, Pillow for images, `unstructured` as
  fallback.
- Config via pydantic-settings / env vars only.

### LLM adapter

All model access goes through one internal interface with two backends:
Anthropic-native, and OpenAI-compatible (covers xAI today, vLLM/Ollama when a
GPU arrives). Every LLM task declares a **task profile** (model tier, max
cost, temperature) so cheap tasks route to cheap models and synthesis routes
to strong ones — per provider, in config. LLM calls never run in tests; the
adapter has a fake implementation.

## Frontend

React + Vite + TypeScript PWA, **mobile-first** (bottom-nav: capture / chat /
search / review inbox). `vite-plugin-pwa`, TanStack Query, Tailwind +
shadcn/ui, markdown editor. Offline note capture via an IndexedDB outbox that
syncs on reconnect. API types generated from the FastAPI OpenAPI schema.

PWAs cannot do continuous background location; GPS tracking uses the
**OwnTracks** native apps posting to our authenticated ingestion endpoint.

## Security model: subjects, principals, domains

- **`subjects`** — who/what data is *about* (me, Dad, Mom, tracked devices).
- **`principals`** — who can *act*: the owner, scoped capability tokens
  (intake links), device API keys (OwnTracks).
- **`domains`** — information firewalls (`general`, `health`, `finance`,
  `location`, …). Every note, chunk, fact, entity, wiki article, and
  structured record carries a `domain_id`.

Enforcement is **Postgres Row-Level Security**: every session carries a
domain-scope set (session GUC) and RLS filters every query at the database
layer — application bugs cannot leak across domains. The owner's sessions
carry all scopes (everything visible by default); tokens and device keys are
scoped to (subject, domain). Wiki builds run per-domain, so a health fact can
never be cited in a general article. Every new table ships with RLS tests
proving a scoped session cannot see other domains' rows.

## Knowledge pipeline

```
note saved → event → extraction (attachments) → multi-granularity chunking
  → embeddings + tsvector → facts & entities (LLM, with citations to chunks)
```

- **Chunks** are multi-granularity (paragraph-level for precision,
  section-level for context) with offsets back into the source note.
- **Hybrid search**: pgvector dense + Postgres FTS, fused with Reciprocal
  Rank Fusion, always domain-scoped. A reranker container can be added later
  if quality demands.
- **Facts** carry `superseded_by` chains. Conflicts resolve newest-wins
  automatically and the pair lands in the review inbox with both citations.
  Superseded facts stay queryable for citation integrity.

## Wiki

Articles, revisions, and citations are Postgres rows; citations are foreign
keys to facts/chunks — enforced data, not markdown convention. The wiki is
**machine-written only**, governed by an editorial config (style guide,
citation-density requirements, split/merge thresholds) stored as data, not
code.

**Incremental builds**: a wiki index (per-article summary + embedding) is
maintained. The nightly job takes only facts created/superseded since the
last run, matches them against the index via hybrid search, and a cheap
triage call per cluster decides update / create / split / merge / ignore.
Only affected articles are rewritten as new revisions. Cost scales with the
day's notes, not corpus size. Split/merge actions require owner approval via
the review inbox.

**Correction loop**: the owner never edits articles. A "discuss this article"
chat, anchored to a revision, produces a **correction note** — a first-class
note citing the disputed revision — which flows through normal ingestion with
elevated extraction weight and queues the article for the next pass.

## Structured records

Not everything is free text, but everything traces to a note:

- **Lists**: `lists` / `list_items`, manipulated by agent tools, pinned in
  the PWA.
- **Lab results**: typed rows (test, value, unit, reference range, date)
  extracted from lab-report attachments, each citing its source note.
  `health` domain.
- **Appointments**: proposed from notes by the analysis pipeline (via review
  inbox), managed by agent tools, published as a read-only **ICS feed** the
  phone's native calendar subscribes to.
- **Location fixes**: Timescale hypertable, per-subject, written by OwnTracks
  device keys; PostGIS geofence transitions emit workflow events.

## Workflow engine

Custom, on Postgres: `events` → `triggers` → `pipelines` (stored definitions
of action sequences) → `runs` (full execution logs). Everything that happens
emits an event — note created, schedule fired, geofence crossed — and the
ingest and wiki pipelines are themselves pipeline definitions, proving the
engine on real load.

## Review inbox

One unified queue in the PWA for everything needing human judgment: fact
conflicts, proposed appointments, wiki split/merge approvals, extraction
corrections. Badge count on the bottom nav; each item resolvable in a tap or
two. Rejecting a fact drafts a correction note.

## Agent

A tool-calling personal agent over the LLM adapter: hybrid search, read
note/entity/fact, manage lists and appointments, propose edits (as correction
notes). Tools respect the session's domain scopes via the same RLS plumbing.
Phone chat UI is a primary interface, not an afterthought.
