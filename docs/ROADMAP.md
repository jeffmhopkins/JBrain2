# JBrain2 — Roadmap

Each phase ends with something used daily. Phases 1–4 make it a daily phone
companion; 5–6 add the self-organizing wiki; 7 extends to family and devices.

## Phase 0 — Foundation

Compose stack boots end to end. Caddy with TLS on the public domain. Postgres
(TimescaleDB-HA image) with Alembic migrations. FastAPI healthcheck. PWA shell
installable on a phone. **`install.sh`** bootstraps barebones Ubuntu (Docker +
deps, secrets, domain + LLM key prompts) and prints the **owner key** —
owner-key auth with device sessions, `jbrain reset-owner-key` recovery.
**Supervisor container** with stack status, restart, and live log streaming
into a minimal Ops screen. `subjects` / `principals` / `domains` tables with
**Row-Level Security wired and tested**. CI (lint, typecheck, tests) plus
image publishing to GHCR (stable on tags, edge on green main). Backup script:
nightly `pg_dump` + blob-volume sync, restore procedure tested once before
any real data exists.

**Exit:** a fresh Ubuntu VM reaches a running, TLS-served stack via
`install.sh` alone; login works with only the printed owner key; the stack
can be restarted and logs tailed from the PWA; a restore from backup has been
performed successfully; RLS tests prove domain isolation.

## Phase 1 — Notes

Note capture via the approved omnibox home (morphing Entry/Medical/
Financial segments, message-send model, day-grouped transcript stream);
attachments (content-addressed storage); offline capture with an
IndexedDB outbox and idempotent sync; card-launcher navigation; dual
theming with Settings. Server updates ship via `jbrain update`
(build-from-source: backup → git pull → rebuild → migrate → restart) and
the Ops screen's one-tap "Update server", which drives the same sequence
through a supervisor-spawned detached updater container.

**Exit:** daily note capture from the phone is habitual, including
offline; `jbrain update` carries a running install forward across a
schema migration.

## Phase 2 — Ingestion & search

Events on note save; job queue + worker; text extraction from attachments;
multi-granularity chunking; embeddings via the `embed` container; hybrid
search (dense + FTS, RRF) with a domain-scoped search UI.

**Exit:** search reliably beats manual scanning; retrieval quality validated
by hand before any LLM consumes it.

## Phase 3 — Analysis

LLM adapter (Anthropic + OpenAI-compatible). Fact and entity extraction on
ingest, with citations to chunks. Supersession chains, newest-wins with
review flag. Entity pages. The **unified review inbox** ships here.

**Exit:** new notes produce reviewable facts/entities with correct citations;
conflicts surface and resolve in the inbox.

## Phase 4 — Personal agent & structured records

Tool-calling agent (search, read notes/entities/facts, lists, appointments)
with phone chat UI. `lists` / `list_items`. `appointments` with
note-extraction proposals and a read-only ICS feed.

**Exit:** the agent is the default way to ask "what do I know about X" and
manage lists/appointments from the phone.

## Phase 5 — Workflow engine

Generalize the hardcoded ingest pipeline into `events` / `triggers` /
`pipelines` / `actions` / `runs`, with a scheduler and run-log UI.

**Exit:** ingest and a scheduled job run as user-defined pipeline
definitions; failures are diagnosable from run logs alone.

## Phase 6 — Wiki

Wiki index (article summaries + embeddings). Incremental nightly builder:
delta facts → index match → triage (update/create/split/merge) → targeted
rewrites with enforced citations → versioned revisions. Editorial config
(style guide, citation requirements) as data. Split/merge approvals via
review inbox. Read-only wiki UI with citation hover-cards. "Discuss this
article" correction-note loop.

**Exit:** a day of notes updates only the affected articles overnight, every
claim cites a note, and corrections happen by out-arguing the wiki with a
correction note.

## Phase 7 — Outer ring

Scoped capability tokens; guided-intake share links (interview agent gathers
e.g. medical history or recipes, sessions become notes attributed to the
right subject and domain). OwnTracks ingestion endpoint with per-device keys;
location hypertable; PostGIS geofence events into the workflow engine.
Lab-report extraction into typed `lab_results`.

**Exit:** a family member completes an intake link unassisted; phones report
location continuously; a photographed lab report becomes queryable rows
citing its note.
