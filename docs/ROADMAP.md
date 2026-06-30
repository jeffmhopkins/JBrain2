# JBrain2 — Roadmap

Each phase ends with something used daily. Phases 1–4 make it a daily phone
companion; 5–6 add the self-organizing wiki; 7 extends to family and devices.

## Status (2026-06)

**Phases 0–4 and the Phase 5 workflow engine are shipped.** Notes,
ingestion/search, the v3 note→graph analysis pipeline (extract → Integrator →
arbiter), and the personal agent (tool-calling loop, Tier-A memory,
Proposals/review inbox, external connectors, the Full Brain chat surface) are all
live; lists and appointments ship with it. The **Phase 5 workflow engine** —
`events`/`triggers`/`pipelines`/`actions`/`runs`, the scheduler, the unified
run-log, and the non-breaking cutover of ingest/integration/consolidation onto the
engine — is also live, with reflexion-in-the-live-turn (Loop 1) and the recurring
self-heal reconcilers; migrations run through 0044. The note-analysis calibration
evals (`docs/CALIBRATION_LOOP.md`) run as a CI quality guard. The build records for
the agent and the v3 pipeline are archived under `docs/archive/` (`ASSISTANT_PLAN.md`,
`INTEGRATOR_PLAN.md`, `CUTOVER_V1_REMOVAL.md`).

**Phase 5 is complete** (the build record is archived at
`docs/archive/PHASE5_COMPLETION_PLAN.md`). The self-improvement Loops 2–4 (skill
learning, durable-knowledge promotion, prompt/tool self-edit) and their eval/promotion
harness were **removed** — only Loop 1 (reflexion) shipped and remains. The
not-yet-built hygiene sweeps are carried into the Phase 6 section below.

## Phase 0 — Foundation ✅ Shipped

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

## Phase 1 — Notes ✅ Shipped

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

## Phase 2 — Ingestion & search ✅ Shipped

Postgres job queue (SKIP LOCKED, backoff, stale-job reaper) + worker loop
with automatic backfill; the attachment analysis dispatcher (text/PDF
chains, OCR seam for P3); paragraph/section chunking with RLS-firewalled
chunks; embeddings via the `embed` container (bge-small-en-v1.5 384-dim on
the 4GB box — model is an env var, re-embed is a planned migration for the
32GB upgrade); hybrid search (dense + FTS, RRF k=60) with FTS-only degraded
fallback. UI: bounded mode-scoped home stream with swipe action rail and
indexing chips, passage-first Search screen with match badges, the
Note/Analysis note view, note edit/delete/move-domain, capture-location
setting.

**Exit:** search reliably beats manual scanning; retrieval quality validated
by hand before any LLM consumes it.

## Phase 3 — Analysis ✅ Shipped

LLM adapter (Anthropic + OpenAI-compatible). Fact and entity extraction on
ingest, with citations to chunks. Supersession chains, newest-wins with
review flag. Entity pages. The **unified review inbox** ships here.

**Exit:** new notes produce reviewable facts/entities with correct citations;
conflicts surface and resolve in the inbox.

*Deferred — fuller entity-correction (later analysis-hardening pass):* the
linking and conflict-surfacing half ships in Phase 3 — declared-name aliasing,
collision → `merge_proposal`, `distinct_from` enforcement, attribute-collision
cards, and the mixed-domain citation firewall. The inverse — **splitting an
over-merged entity** (an attribute collision as a hidden two-people signal →
`split_proposal`, with provenance-based re-partition of the entity's
mentions/facts into the new identity) and **alias-detach** (removing a
wrongly-attached name and re-resolving the mentions it linked) — is left for a
later pass; the merge machinery's reversible-effects pattern is the model to
mirror. Bare-first-name retro-recheck and layer-3 `distinct_from` are **not on
the path** — they would only matter under same-name entity coexistence, which
was evaluated and **rejected** (docs/ANALYSIS.md "Same-name coexistence"): the
conservative exact-collision → review card is the correct, safer answer for a
single user, so the human-initiated split above is the only entity-correction
worth building.

## Phase 4 — Personal agent & structured records ✅ Shipped

Tool-calling agent (search, read notes/entities/facts, lists, appointments)
with phone chat UI. `lists` / `list_items`. `appointments` with
note-extraction proposals and a read-only ICS feed.

**Exit:** the agent is the default way to ask "what do I know about X" and
manage lists/appointments from the phone.

## Phase 5 — Workflow engine ✅ Shipped

Generalize the hardcoded ingest pipeline into `events` / `triggers` /
`pipelines` / `actions` / `runs`, with a scheduler and run-log UI. The engine,
scheduler, run-log, cutover, reflexion-in-the-live-turn, and the self-heal
reconcilers all shipped (migrations through 0044; build record in
`docs/archive/PHASE5_COMPLETION_PLAN.md`). The carried-forward items below all
landed or were deliberately seamed/deferred. The self-improvement Loops 2–4 and
their eval/promotion harness were **removed** (only Loop 1 / reflexion remains).

**Carried forward from Phases 3–4** (deferred deliberately, picked up here):

- **`extraction_truncated` review card** — the per-note fact cap still fires
  under `integrate_note`, but `plan_to_extraction` rebuilds the `Extraction`
  with `dropped_facts=0`, so no card is surfaced. Restore the user-facing card.
  (`docs/archive/CUTOVER_V1_REMOVAL.md`, `docs/archive/INTEGRATOR_PLAN.md`.)
- **`integration_run` + `resolution_pin` tables** — the Integrator turn-loop
  logs to structlog only and re-run convergence rides the arbiter's
  deterministic signals; persist the run + memoize identity/predicate decisions
  for auditability and convergence (becomes a workflow `run`). (N9/N10.)
- **N14 owner-ahead ordering** — `backfill_pending_integration` is oldest-first
  by `created_at`; the `provenance` column exists but isn't wired into the sort,
  so untrusted-origin notes aren't yet processed behind owner notes.
- **Agent-loop maturation** — auto-wire reflexion into the default turn,
  surface `job_enqueued` for deferred/long tools, and add the `.tool`
  version-bump CI guard (mirroring the `.prompt` guard).

**Scheduled-task migration [note]:** by this phase, find every periodic or
swept task that today runs as an ad-hoc boot self-heal or hardcoded handler —
**predicate consolidation** (the `consolidate_predicates` action,
docs/entity.md), entity hygiene, merge proposals, summary re-embedding, tag
consolidation, and the nightly wiki build — and move them onto the engine's
`events → triggers → pipelines → actions → runs`, defined as data. Each must be
**on-demand ("emergency") triggerable**: a sweep becomes a run-logged action a
human can fire immediately from the Ops/review surface, not a service restart.
The actions are built first (they work as enqueued jobs today); this phase only
gives them their scheduled and manual triggers.

**Exit:** ingest and a scheduled job run as user-defined pipeline
definitions; failures are diagnosable from run logs alone.

## Phase 6 — Wiki — Planned (build plan: `docs/PHASE6_WIKI_PLAN.md`)

The LLM-maintained wiki, and **only** the wiki. Wiki index (article summaries +
embeddings). Incremental nightly builder: delta facts → index match → triage
(update/create/split/merge) → targeted rewrites with enforced citations →
versioned revisions. Editorial config (style guide, citation requirements,
per-type guides) as data. Split/merge approvals via the review inbox. Read-only
wiki UI with citation cards. "Discuss this article" correction-note loop (Talk).
A living, search-first landing; search extended to include articles. See the
detailed build plan for the data model, the four engine actions, the writing-style
spec, the firewall design, and the cross-stream `PHASE6_WIKI_GRAPH_CONTRACT.md`.

**Exit:** a day of notes updates only the affected articles overnight, every
claim cites a note, and corrections happen by out-arguing the wiki with a
correction note.

## Phase 6 follow-ons — Planned (separate multi-wave plans)

Each is its own multi-wave plan — some unblocked *by* the wiki spine, some
independent agent-infrastructure that can run alongside it (folding any of them
into the wiki broke one-PR-per-wave and hid the true size). *(The
self-improvement Loops 2–4 once listed here — skill learning, durable-knowledge +
predicate-canon promotion, and prompt/tool self-edit — and their eval/promotion
harness were removed, not deferred.)*

- **Hygiene sweeps** (build plan: `docs/HYGIENE_SWEEPS_PLAN.md`) — entity hygiene
  (`entity_hygiene`: delete provisional orphans stranded by retraction/supersession),
  summary re-embedding (`reembed_stale`: re-embed stale-model entities), tag
  consolidation (`tag_consolidate`: fold drift tag spellings) — built as engine actions
  on the Phase-5 sweep pattern, seeded disabled + Ops-fireable. Distinct from the wiki's
  own `wiki_reindex` (which only re-embeds wiki summaries).
- **Sub-agent spawning** (build plan: `docs/SUBAGENT_SPAWNING_PLAN.md`) —
  **agent-infrastructure, independent of the wiki spine** (parallel-safe; does not
  wait on the entity-graph rebuild). Expands the reserved `spawn_subagent` hatch
  (`docs/ASSISTANT.md`) into a bounded fan of web-sandboxed
  research/review/summarize sub-agents spawned by `jerv`: parent-authored brief as
  data, child tools/scope ⊆ parent, depth ≤ 2, a direct caps-bounded fan, a shared
  tree budget, and live streaming into the chat + a nested session tree. Scheduled
  as its own multi-wave plan (waves **S1–S4**); design-complete and through a
  three-lens adversarial review.

## Phase 7 — Outer ring — Planned

Scoped capability tokens; guided-intake share links (interview agent gathers
e.g. medical history or recipes, sessions become notes attributed to the
right subject and domain) — **build plan: `docs/GUIDED_INTAKE_PLAN.md`** (five
waves, GUI mock gate cleared). OwnTracks ingestion endpoint with per-device keys;
location hypertable; PostGIS geofence events into the workflow engine.
Lab-report extraction into typed `lab_results`.

**Exit:** a family member completes an intake link unassisted; phones report
location continuously; a photographed lab report becomes queryable rows
citing its note.
