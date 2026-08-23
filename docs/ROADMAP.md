# JBrain2 — Roadmap

> **Status:** Living · **Last verified:** 2026-08-23

Each phase ends with something used daily. Phases 1–4 make it a daily phone
companion; 5–6 add the self-organizing wiki; 7 extends to family and devices.

## Status

**Phases 0–4 and the Phase 5 workflow engine are shipped.** Notes,
ingestion/search, the v3 note→graph analysis pipeline (extract → Integrator →
arbiter), and the personal agent (tool-calling loop, Tier-A memory,
Proposals/review inbox, external connectors, the Full Brain chat surface) are all
live; lists and appointments ship with it. The **Phase 5 workflow engine** —
`events`/`triggers`/`pipelines`/`actions`/`runs`, the scheduler, the unified
run-log, and the non-breaking cutover of ingest/integration/consolidation onto the
engine — is also live, with reflexion-in-the-live-turn (Loop 1) and the recurring
self-heal reconcilers. The note-analysis calibration
evals (`docs/archive/CALIBRATION_LOOP.md`) run as a CI quality guard. The build records for
the agent and the v3 pipeline are archived under `docs/archive/` (`ASSISTANT_PLAN.md`,
`INTEGRATOR_PLAN.md`, `CUTOVER_V1_REMOVAL.md`).

**Phase 5 is complete** (the build record is archived at
`docs/archive/PHASE5_COMPLETION_PLAN.md`). The self-improvement Loops 2–4 (skill
learning, durable-knowledge promotion, prompt/tool self-edit) and their eval/promotion
harness were **removed** — only Loop 1 (reflexion) shipped and remains. The
not-yet-built hygiene sweeps are carried into the Phase 6 section below.

**Inline approvals** ✅ (`docs/archive/INLINE_APPROVALS_PLAN.md`, migration 0130): Proposal
approval moved from the side panel **into the conversation** (variant D,
`docs/mocks/inline-approvals/`) — per-leaf approve / decline-with-reason / correct-in-place
and a single Enact that **returns a consolidated, server-authored outcome to the assistant
so it follows up** (the former "deferred concept" in `FullBrainSurface.tsx`). The panel
remains for browsing older / cross-session proposals.

**Report share links** ✅ (`docs/archive/RESEARCH_SHARE_LINKS_PLAN.md`, migration 0150, GUI
variant B): the owner mints a public, revocable, no-login link to one research report or a whole
folder; anyone opens it at `/share/<token>`. The read is a dedicated RLS scope
(`research_reports_share`) over an empty-scope share context — the token resolves a pin, the
database decides what's visible — and the public `/api/research-share` router is rate-limited,
`no-referrer`, and `noindex`.

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
was evaluated and **rejected** (docs/reference/ANALYSIS.md "Same-name coexistence"): the
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
reconcilers all shipped (build record in
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
docs/reference/entity.md), entity hygiene, merge proposals, summary re-embedding, tag
consolidation, and the nightly wiki build — and move them onto the engine's
`events → triggers → pipelines → actions → runs`, defined as data. Each must be
**on-demand ("emergency") triggerable**: a sweep becomes a run-logged action a
human can fire immediately from the Ops/review surface, not a service restart.
The actions are built first (they work as enqueued jobs today); this phase only
gives them their scheduled and manual triggers.

**Exit:** ingest and a scheduled job run as user-defined pipeline
definitions; failures are diagnosable from run logs alone.

## Phase 6 — Wiki — In progress (build plan: `docs/plans/PHASE6_WIKI_PLAN.md`)

The LLM-maintained wiki, and **only** the wiki. Waves A–C are shipped: wiki
index (article summaries + embeddings) ✅; incremental nightly builder — delta
facts → index match → triage (update/create/split/merge) → targeted rewrites
with enforced citations → versioned revisions ✅; editorial config (style guide,
citation requirements, per-type guides) as data ✅; split/merge approvals via
the review inbox ✅; read-only wiki UI with citation cards ✅; "Discuss this
article" correction-note loop (Talk) ✅; a living, search-first landing with
search extended to include articles ✅. Remaining: Wave D — re-enable the
nightly schedules, grounding-gate tuning, purge→rebuild. See the detailed build
plan for the data model, the four engine actions, the writing-style spec, the
firewall design, and the fulfilled cross-stream contract
(`docs/archive/PHASE6_WIKI_GRAPH_CONTRACT.md`).

**Exit:** a day of notes updates only the affected articles overnight, every
claim cites a note, and corrections happen by out-arguing the wiki with a
correction note.

## Phase 6 follow-ons — Shipped (build records under `docs/archive/`)

Each shipped as its own multi-wave plan. *(The self-improvement Loops 2–4 once
listed here — skill learning, durable-knowledge + predicate-canon promotion, and
prompt/tool self-edit — and their eval/promotion harness were removed, not
deferred.)*

- **Wiki health sweep (`wiki_lint`)** ✅ (`archive/WIKI_LINT_PLAN.md`) — the "third
  leg" alongside ingest and query: a corpus-wide wiki HEALTH audit as a fifth in-code
  sweep `ActionSpec`, read-only against the wiki. Wave A (deterministic no-LLM checks +
  optional index re-dirty, migration 0119); Wave B (the LLM contradiction/stale-claim
  review cards + a separate lint budget, migration 0120). **Enabled on the nightly
  schedule by migration 0121** (04:00, after the re-enabled builder refresh/prune);
  still Ops-fireable on demand.
- **Hygiene sweeps** ✅ (`archive/HYGIENE_SWEEPS_PLAN.md`) — `entity_hygiene`,
  `reembed_stale`, `tag_consolidate` engine actions on the Phase-5 sweep pattern,
  seeded disabled + Ops-fireable (migration 0066).
- **Sub-agent spawning** ✅ (`archive/SUBAGENT_SPAWNING_PLAN.md`,
  `archive/SUBAGENT_FEEDING_WAVES_PLAN.md`) — `jerv` fans out web-sandboxed
  research/review/summarize sub-agents (`agent/spawn.py`, migration 0105).
  *Deferred:* feeding-wave run-log persistence + live SSE.

## Phase 7 — Outer ring — Mostly shipped

The location + family + intake slices shipped; build records are under
`docs/archive/`.

- **Guided-intake share links** ✅ (`archive/GUIDED_INTAKE_PLAN.md`) — owner-minted
  interview links → owner-approved intake submissions → attributed notes
  (migrations 0107–0113).
- **Location** ✅ (`archive/PHASE7_LOCATION_PLAN.md`, `_LOCATION_DETAIL_PLAN.md`) —
  OwnTracks ingest with per-device keys, location hypertable, geofence events into
  the workflow engine, motion-adaptive dense trails (migrations 0059–0064/0073).
- **Family tracker + app map** ✅ (`archive/PHASE7_FAMILY_TRACKER_PLAN.md`,
  `_APP_MAP_PLAN.md`) — MQTT ingest, pairing/view-scope, the live member map
  (migrations 0067/0075). *Deferred:* the M7c ops runbook. (Push notifications
  shipped **self-hosted** — the Android owner app's `NotificationRelayService`
  holds an SSE connection to `/api/notifications/stream`; there is no Firebase/FCM
  in the codebase, so the earlier "Android FCM registration hardening" line is
  moot.)
- **Location assistant** ✅ (`archive/LOCATION_ASSISTANT_PLAN.md`) — owner-only
  `where_is`/dwell/`save_place` tools. *Deferred:* the L5 dwell segmenter (waits
  on the analytics tier).

- **JPet — the family wall play-pet** ✅ (`archive/JPET_PLAN.md` v1, `archive/JPET_V2_PLAN.md` v2,
  `archive/JPET_V3_PLAN.md` v3)
  — a Tron/synthwave **3D** wireframe robot the kids make *do things* and talk to. One
  server-authoritative `pet_state`, two surfaces: the **phone Control** screen in the PWA (big kid
  play-buttons, push-to-talk, a grown-ups room-map) drives it via `POST /api/pet/command`, and the
  **wall view lives on the on-box wall display** (`deploy/wall`, `:8800/pet`) — a
  read-only WebGL room that polls the pet through the internal-only `GET /internal/pet` (Caddy never
  routes `/internal` off-box), so the display stays DB-free and the pet is never exposed publicly.
  **v2 (migration 0125)** pivoted from Tamagotchi decay to positive, command-and-response play: the
  `pet.turn` brain emits a bounded, enum-constrained **action-script** ("chase the ball", "pick up
  the ball and put it in the corner") the wall plays out; **room objects** (ball/bed/toy-box/food-
  bowl/ball-pit/light-switch) with object-targeted actions + carry; per-action WebAudio **sound**
  cues + piper speech + **day/night** lighting; happy meters that never decay; capped ambient life.
  `pet_memory` (it remembers you); play off the job queue (second seat); scoped pet + kid principal
  firewall (never health/finance/location). Migrations 0123–0125. **v3 shipped**
  (`archive/JPET_V3_PLAN.md`): the wall became the pet's continuous real-time brain. **W1**
  (migration 0126) — an autonomy engine so it's *constantly, fluidly* alive on its own (constrained-
  randomness behaviour + damped-spring motion + always-on idle micro-motion), the drive meters
  **ripped out** (mood reads from behaviour), **solid-wireframe** rendering, and a **2×** room.
  **W2** — the living, interactive world: **ball physics** + **mouse click-to-play**, the
  **block-builder** (small coloured solid bricks → big varied statues, knocked down by the ball +
  rebuilt), **detailed furniture** + **TV** + **window**, **circadian** day/night (sleepy at night),
  and a **vacuum** tool. **W3** — reliable **hybrid talk→action** router (keyword-first, LLM never
  500s), **colour-on-command** (rainbow cycling), and two activities: a **jump rope** and a
  **playable synth** (clickable pentatonic keys, WebAudio, the pet plays it), plus the phone
  Control colour palette + activity buttons (migration 0127).

**In progress:** EMR / medical-record import (build plan: `docs/plans/EMR_IMPORT_PLAN.md`) —
multi-system EMR PDF exports (Epic / OneContent / athena / scanned-OCR), fed as one note with an
encrypted zip + inline password, normalized in place into cited, health-firewalled `measurement`
and `event` facts, surfaced through `lab_results`/`encounters` projections and
`read_labs`/`read_encounters` tools. Waves 0–3 shipped (the per-system parsers, deterministic
integration, the `emr_projection`, the `read_labs`/`read_encounters` tools, and encrypted intake +
the two-stage `emr_import`/`emr_parse` worker pipeline); W4 partial (the analyte-currency flag
landed); W5 (Phase-6 wiki surfacing) blocked on Phase 6. Subsumes the earlier "typed `lab_results`"
line (a photographed lab report becomes queryable rows citing its note).

**Shipped:** Local LLM prompt cache (build record: `docs/archive/LLM_PROMPT_CACHE_PLAN.md`) — cut
on-box first-token latency by keeping the static jerv/curator system-prompt prefix reusable across
turns: W1 made the prompt prefix byte-stable (moved the volatile `now_block`/presence to the tail,
`api/agent.py`), W2 turned on llama-server's `--cache-reuse 256` (`llm/llama_swap_config.py`). Both
waves shipped, no migration. A `--slot-save-path` disk cache was later built and then **removed**
(2026-08-21): inert by construction on a hybrid, and on gpt-oss it persisted whatever held the
single slot rather than the prefix. The in-RAM prompt cache went with it (`-cram 0`) to buy
co-residency. The `-np`/`--parallel` second slot is operator-settable per model.

**In progress:** Entity-graph ingest V2 (build plan: `docs/plans/ENTITY_GRAPH_INGEST_V2_PLAN.md`) —
cut the ingest review-inbox noise the owner hit without changing the pipeline structure: remove the
inferred-ceiling review trap + the eight arbiter backstops (Lever A), default `state`/functional-
`relationship` conflicts to non-destructive supersede-with-history by validity time while `attribute`
collisions stay review as the hidden-two-people-merge signal (Lever B), and let a structured
review-card correction write its pinned override directly instead of minting a prose note (Lever C).
Re-run determinism stays deterministic recomputation (no cached verdict); the firewall/RLS/namesake
spine is unchanged. §11 decisions ratified; on-box gpt-oss-120b validation showed the ingest-quality
gap is prompt+schema, not architecture (agentic + multi-tier ingestion evaluated and rejected). The
V0 local-box judgment spike is largely done; V1 (deterministic enactor + safety spine, tasks
T1.1–T1.5) landed (#944); V2 through V5 (cutover + Lever C + docs reconciliation) are open. Corrects-in-place `reference/ANALYSIS.md`
(per-kind conflict policy) and `reference/ENTITY_GRAPH_REFOCUS_PLAN.md` (the `INFERRED_CEILING`
rationale) when it builds.

**In progress:** External video ingestion (build plan: `docs/plans/EXTERNAL_VIDEO_INGESTION_PLAN.md`)
— any analysed YouTube video (ad hoc or scheduled) lands in an isolated, embedded, searchable corpus
(`external_sources`/`external_source_chunks`), deliberately kept out of the knowledge graph/wiki since
third-party content is not a source of truth. Built on the shipped `analyze_stream` + captions-first
(#879). Waves W1–W2 shipped (the corpus tables, a timeline windower, the `analyze_stream` write-through,
and the sandboxed-`jerv` `search_external_video` + `check_channel` tools); W3 (a recurring Jerv Task for
scheduling — no workflow-engine machinery) open.

**In progress:** Deep research tool (build plan: `docs/plans/DEEP_RESEARCH_TOOL_PLAN.md`) — a
dedicated jerv-only `deep_research` tool that turns one question into a structured, cited report by
orchestrating the existing web-sandboxed sub-agent fan across a bounded
plan→gather→reflect→one-refill→synthesize→critique/revise run (a complexity skip matrix + tiered
source-quality corroboration borrowed from `kyuz0/deep-research-agent`, the owner's local-model
reference). The honest generalization of feeding waves — bounded, in-request, one owner turn — reusing
`spawn_fan`, the tree budget, and the research/review personas unchanged. All three waves landed
on-branch (D1 spine, D2 refill round + critique, D3 the `deep_research_report` tool-view + jerv
steering) with full backend + frontend unit suites; the D3 mock-gate sign-off and on-box budget /
wall-clock tuning remain before it is marked settled.

**Shipped:** Deepest research (build record: `docs/archive/DEEPEST_RESEARCH_TOOL_PLAN.md`) — a
no-holds `deepest_research`: an autonomous, resumable background run that recurses two agent tiers
(orchestrator → task agent → sub agent), loops until covered or an owner-set token/wall-clock
ceiling, checkpoints its state, streams periodic progress back to the initiating chat, and lands a
cited report in the existing `research_reports` library. Red-teamed across five adversarial reviews;
§4's brief-laundering egress-exfil channel was closed as a hard R2 build blocker. Build waves R1–R8
shipped (migrations 0146–0148: the `research_deep` persona, `research_run_state`, tool-aware
`(question_hash, tool)` dedup) — the background lane, two-tier recursion, checkpoint/resume, and
progress channel are all live. *Deliberately skipped:* R0, a value-probe/kill-gate the owner
overrode to build the full stack — it never fired, and the coverage-gap probe it would have run is
not on the path.

**Shipped:** Deep research — video-library source modes (build record:
`docs/archive/DEEP_RESEARCH_VIDEO_SOURCES_PLAN.md`) — a `sources` knob on the `deep_research` tool so
a run can draw from the owner's external video library (`external_sources`/`external_source_chunks`) instead
of, or ahead of, the open web: `web` (default, unchanged), `library` (exclusive to the video corpus), and
`library_first` (the library is the primary gather pass; the web fills only the reflect→refill gap round).
Structural, not prompt-steered — `sources` only selects the gather/refill child persona (`research` vs. a new
corpus-searching `research_library`); the plan→gather→reflect→refill→synthesize→critique machine, the tree
budget, and the report view are otherwise unchanged, and video hits already cite as timestamped `[^n]`
`WebSource` chips. Explicitly **not** the deferred KB-scoped deep research (the owner's notes/wiki/entities
stay out of scope); the video corpus is non-sensitive third-party content jerv already reads safely. DV1
(routing + flag — the `sources` param, the `research_library`/`review_library` corpus personas via
migration 0144, and per-mode gather/refill/review routing), DV2 (jerv steering, `source_mode` persistence
via migration 0142, and the report-view provenance chip), and DV3 (the source-mode chip) all shipped.
*Open follow-ons:* an attachment-video "fourth mode" and library-mode breadth tuning.

**In progress:** Deep produce (build plan: `docs/plans/DEEP_PRODUCE_PLAN.md`) — generalize the shipped
`deep_research` pipeline into one `produce()` engine behind an abstraction layer, surfaced as two
verbs: `deep_research` (the report preset, behavior-preserving) and `deep_produce` (a caller-supplied
`Directive` — objective + `output_kind` — producing a plan/table/brief/differential/timeline). Access
is a seed-keyed `SourcePlan`, not a persona field the engine cannot read: the engine branches on whether
it assembled a health/KB seed, giving a three-way seeded / refuse / plain-produce split that makes the
`seed ⇒ ¬web ∧ ¬external-sink` exfiltration invariant self-enforcing. **W1 delivers a standalone jerv
capability** — `deep_produce` over web/library to any artifact, external sink — whose value is
independent of the health use case, gated only by `deep_research`/`deepest` byte-stable regression (both
drive the same `research()` method, so the refactor keeps one `_run` implementation with `on_round`/
`require_persist` intact). W2 adds the curator seeded path (EMR facts seeded under RLS in the parent,
web-fan suppression, fail-closed grounding refusal, `external`-write suppression, non-report render) under
a narrow, documented carve-out to `EMR_IMPORT_PLAN.md`'s no-clinical-decision-support line; W3 is the
recipe registry + owner UI. Adversarially reviewed (42 findings, 19 confirmed after verification) and
hardened before scheduling. **W1 shipped** (PR #965 — the standalone jerv `deep_produce` verb, the
single-`_run` refactor, `output_kind` shaping byte-stable for reports); **W2 shipped** — the curator
seeded/health path: `_assemble_emr_seed` reads labs+encounters under RLS in the parent, the seeded run
pins `library` mode (web-fan impossible), refuses a non-health/empty read, and suppresses `external`
writes, with a critic that verifies the seeded plan against the record. W3 (recipe registry + owner UI)
open.
The motivating recipe: `deep_produce(output_kind=plan)` from a
curator call — a hypothetical treatment plan grounded in a medical-history date range and a library
category, if symptoms were to recur.

**Shipped:** jerv planning tool (build record: `docs/archive/JERV_PLANNING_TOOL_PLAN.md`) — an
owner-approved, per-conversation **plan** jerv drafts (only when the owner asks), the owner alone
approves (jerv can never self-approve — the anti-injection guard), then executes across turns. A
jerv-only `read_plan`/`write_plan`/`write_plan_result` trio over an owner-only `agent_session_plans`
row (migrations 0155, 0157–0159), the approved plan re-injected each turn, and a bounded,
owner-interruptible auto-continuation loop that streams each step live, isolates multiple plans per
chat, records per-step results + timing, and reconciles on reopen. Waves P1–P9.5 shipped.
*Deferred:* post-approval body edits are not re-gated — a future "changed since approval" signal
(a body hash captured at approve time) is noted, not built.

**Shipped:** Dynamic portal fetch (build record: `docs/archive/DYNAMIC_PORTAL_FETCH_PLAN.md`) —
resolver adapters (codename "dinosaurs") that let the research fan actually *query* dynamic JS/POST
government search portals (FL Sunbiz corp search, FL DFS licensee search) through the SSRF-guarded
`WebFetcher.submit_form` (no headless browser), surfaced as `WebSource`s in the `[^n]` registry, plus
a precision `_is_search_form_page` detector so an un-adaptered portal degrades honestly. P1–P3
shipped. *Open follow-ons:* per-resolver caching/throttle tuning, a per-adapter post-deploy smoke
route, a scripted-browser tier for a genuinely JS-only portal, and more jurisdictions.

**In progress:** Tavily fetch tier (build plan: `docs/plans/TAVILY_FETCH_TIER_PLAN.md`) — a fourth,
hosted `web_fetch` recovery tier (Tavily Extract) after direct → reader → byparr, catching managed-wall
/ paywall / JS-shell pages the on-box browser sidecars miss **without adding a sidecar** the no-terminal
owner (non-negotiable #10) can't debug. `_fetch_via_tavily` mirrors the reader seam — owner-pinned base
URL, SSRF-guarded public target, `_is_challenge_page`/`_is_paywall_page` guards on the output, windowed
by `_window_and_find`. Single-owner box, so it **ships enabled** and is operated **entirely from the
PWA**: the API key + a manual on/off toggle live in **Settings** (the Gmail-credentials precedent —
secret stored via GUI, never echoed, `JBRAIN_TAVILY_API_KEY` env fallback), read live per fetch via a
settings-provider seam into the fetcher, with a no-terminal "Test key" button on the `tier="tavily"`
debug route. Inert until a key is entered (keyless box byte-unchanged); third-party exposure bounded to
already-blocked URLs, the toggle its instant off switch. **Learns byparr-failed domains**: when the
on-box stack (direct→reader→byparr) fails but Tavily recovers the page, the domain is recorded
(`solver_failed`, a new reason on `app.blocked_domains` migration 0163, with the same 24h lazy-TTL
re-probe) so future fetches route **straight to Tavily**, skipping the doomed on-box legs — the learned
form of the static `solver_first_domains` shortcut, inert while Tavily is off. Extract-only: SearXNG
stays the sole search backend (Tavily Search scoped out — per-credit deep-research cost + loss of
infoboxes/instant-answers). T1 ✅ (tier + settings key/toggle + live provider + `tier="tavily"` debug
selector, headless), T2 ✅ (learned Tavily-first routing: the `solver_failed` reason + record trigger +
route), T3 ✅ (the Settings GUI — dedicated `/settings/tavily` never-echoed endpoint + panel + three
mocks, owner chose mock B (status pill + switch)), T4 ◻️ (live on-box validation + `extract_depth`/timeout tuning +
24h re-probe confirmation).

**Shipped:** Blocked-domain skip list (build record: `docs/archive/DOMAIN_HEALTH_PLAN.md`) — a global
24h paywall/bot-wall skip list (`app.blocked_domains`, migration 0163) so `web_fetch`/`web_search`
stop wasting calls on a domain that just proved unreadable; precision `_is_paywall_page` + the
existing bot-wall detector record the host for 24h, later fetches short-circuit and later searches
drop it, never recording a 404/transient/search-form. *Deferred:* auto-recording the `unreadable`
empty-JS-shell terminal (reserved, held back to avoid false positives), and running the paywall check
on the reader/solver recovery path (today it runs only on the direct fetch).

**Shipped:** Research-report expiry (build record: `docs/archive/REPORT_EXPIRY_PLAN.md`) — opt-in TTL
for research-library reports plus the per-run dedup-key fix that makes it useful: `expires_at`
(migration 0161) stamped from a preset's `retention_days`, a nightly `expire_research_reports` sweep
(migration 0162), an auto-supplied `{{today}}` render variable so `daily_news` dates each day
distinctly and self-expires after 7 days, and a "Keep this report" action that clears the TTL. W1–W4
shipped.

**Shipped:** Deep research staged single-source pipeline (build record:
`docs/archive/DEEP_RESEARCH_STAGED_PIPELINE_PLAN.md`) — teach the shipped `deep_research`/`deep_produce`
engine a **staged, dependency-aware** pipeline so it can process a *single known source*, the motivating
case being "extract every question from this interview, answer each, fact-check each against the web, and
tabulate." Today the engine flattens that sequential job into a **parallel fan of independent angles**
(`deep_research_plan.prompt` even mandates sibling independence), which fails three ways on a real run over
the 85-minute *Economist* Elon Musk interview: no coordination (the answers agent re-derived its own
divergent question list, re-searching the same transcript), fact-check reached no web (`library_first` runs
the whole gather fan corpus-only — the fact-check child literally returned "I can only access the video
library"), and extraction stopped at 41:30 of 85 min (the 60k `read_external_video` cap plus a "stop early"
persona prompt). **W1 lands the observability first** — child sub-agent tool-call + reasoning persistence
(`_persist_child` writes `tools=[]` today, so none of this was visible from storage); it is the instrument
that verifies W2. W2 adds the staged/feed-forward runner (reusing the inert-data feeding-waves envelope
gather→gather) with a per-stage persona (extract=library, answer/fact-check=web under `library_first`); W3
adds the single-source primitives (windowed transcript read, enumeration mode, `output_kind=table`, jerv
routing). Additive throughout — a single-stage plan is byte-identical to today's flat gather, so the report
path cannot regress. W1–W3 shipped (PR #966); each wave independently adversarially reviewed.

**In progress:** Video/image inspection tools (build plan: `docs/plans/VIDEO_IMAGE_TOOLS_PLAN.md`) —
give jerv eyes on a specific still so a visual question is answered from pixels it actually saw, not a
guess. `grab_frame` (persist a frame from a video URL/attachment at time T, optional inline `question`
to grab-and-read in one hop), `fetch_image` (per-hop-SSRF-guarded, validated web-image fetch — jerv
was previously blind to web images), a 2..N-source widening of `analyze_image` (+ a `compare_images`
sidecar) that always emits an owner-visible side-by-side, and a `show: false` flag to suppress the
analyze-video/stream card on intermediate steps. Grabbed/fetched stills are first-class chat images
(`generated_images` + a nullable `provenance` column). Motivated by a real session where jerv
fabricated an image comparison it had no way to perform; reconciled with a four-lens review. V0 (the
`analyze_stream` `single`-mode `seek` fix — it dropped `seek` and always sampled t=0) shipped
on-branch; V1–V6 open.

**In progress:** Cross-turn tool results (build plan: `docs/plans/CROSS_TURN_TOOL_RESULTS_PLAN.md`) —
give jerv durable, referenceable memory of an expensive tool result so a `web_fetch` page (and its
paging position) survives across conversation turns instead of evaporating at turn's end. Motivated by
an observed session where jerv, asked for a fetched YouTube transcript, re-fetched the top each turn,
re-emitted the same section, reached for the wrong (library-only) tools, and fabricated between
windows — all because chat history carries only text. A generic, opt-in **tool-result artifact**
substrate modeled on the turn-attachment subsystem: a session-scoped RLS-firewalled row + a
content-addressed blob for the heavy text (migration 0151), a DATA-framed cross-turn reference line
injected in the volatile suffix, and a `read_artifact` tool that pages the cached text from a
remembered cursor (so "next section" works). W0 (a prompt/description stopgap that stops the
tool-confusion + fabrication) and W1 (the substrate + `web_fetch`/YouTube adoption) landed together;
W2 (`ocr` / `gmail_read` adoption — proving the base is generic) + W3 (polish, owner artifact chip,
optional turn-binding replay) open. A separately-tracked de-dup the research surfaced — unifying the
near-identical research-report library and external-video corpus — is out of scope here.

**Shipped:** Kokoro TTS consolidation (build record: `docs/archive/KOKORO_TTS_CONSOLIDATION_PLAN.md`,
PR #1068) — standardized read-aloud on **Kokoro only** (removed Piper across box/backend/frontend/wall/
docs; `piper_server.py`→`tts_server.py`; browser-native the sole fallback), made `speakable.js` the
single source of truth for text normalization, made the misaki-vs-espeak phonemizer path **visible**
(`/tts/health` + `/api/brain/tts/health`), and gave the owner a **plain-respelling pronunciation list**
(sanitized settings key, applied by the `/api/brain/tts` proxy; the Pronunciations settings panel, mock
A). Fixed the reported symptoms at root — the "U.S." pause (dotted-initialism collapse + streaming
guard), flat headings (colon lead-in), and "Titusville" (engine-agnostic respelling + espeak-path
visibility) — plus coverage gaps (inequalities, snake_case, times, phones, versions, ISO dates).
**Carried deferrals:** (1) the *literal* single-normalizer merge — fully deleting the box
`_speakable_text` by having the wall adopt `speakable.js` verbatim — was rejected as net-worse (it needs
an untestable no-build-step wall rewrite + two byte-identical copies behind a parity guard); the box
keeps a thin engine-agnostic mirror for the wall path instead. Revisit only if the wall is rebuilt with
a bundler or needs the full verbalization set. (2) The box changes (`tts_server.py`, Dockerfile,
entrypoint) require a `jbrain update`/rebuild to take effect; after deploy, verify `/tts/health` reports
`g2p: misaki` (an `espeak` fallback there means the misaki venv isn't loading on the box — the residual
root cause behind a still-wrong "Titusville").

**Shipped:** Grokipedia tool (build record: `docs/archive/GROKIPEDIA_TOOL_PLAN.md`, PR #993) — a jerv
tool set (`grokipedia_search`/`outline`/`section`/`citations`/`related`) to search Grokipedia, traverse a page
by its table of contents, drill into single sections without loading the whole article, and pull
citations the agent can follow to primary sources. Open-internet access only (no xAI key): **API-first**
via Grokipedia's own `/api/typeahead` + `/api/page-preview`, with the server-rendered `/page/<slug>` HTML
as the automatic fallback, both parsed into one surface-agnostic `{outline, section, citations}` model
(`jbrain.web.grokipedia`) with a per-slug cache. Research dossier:
`docs/archive/research/grokipedia-tool/RESEARCH.md`. W1–W3 landed together in PR #993.

**Shipped:** Research Library (build record: `docs/archive/RESEARCH_LIBRARY_PLAN.md`, PR #907) — the
owner's card-launcher browse door to the two `external`-corpus artifacts jerv produces on its own
turns: deep-research reports (`research_reports`) and video analyses (`external_sources`). A
`ResearchScreen` (GUI variant B — segmented Reports/Videos tabs) to search, view, and delete them
over an owner-gated `/api/research-library` HTTP API that reuses the existing corpus
read/search/fetch/delete callables (no migration, no new grant — both tables already carry the DELETE
grant + `external`-domain RLS), plus a detail layer (report via `<Markdown>`, video via
`<VideoAnalysis>`) and per-item actions (open-in-jerv / copy / download / open-source). **Carried
deferrals:** the browse filter is an instant client-side filter over the loaded page — the hybrid
**server** search endpoints ship + are tested but are not yet wired to a *whole-library* search
affordance; **"Open in jerv" seeds a fresh conversation** rather than deep-linking a report's
originating `session_id` (not exposed by the fetch); and a **served-thumbnail route** for external-video
frames, **bulk-delete/select mode**, and **re-run-analysis from the library** remain follow-ons.

**In progress:** Local model ledger (build plan: `docs/plans/LOCAL_MODEL_LEDGER_PLAN.md`) — a
reservation ledger for local-model memory (one row per model instance; charge at intent, discharge
only at confirmed death). L0–L2b shipped — enforcement flipped on 2026-08-23 after a live roster
sweep — L3 (retire the duplicate host-RAM reserves) open.

**In progress:** Local model access (build plan: `docs/plans/LOCAL_MODEL_ACCESS_PLAN.md`) — one way
in, one way out for local-model loading. W0 and W2 partially landed (the compile-error-if-half-wired
admission coordinator; the unadmitted debug-console loads now admit); the access-point collapse,
W1 (caller identity), W3 (five-consumer accounting), and W4 (delete the Anthropic/xAI providers) open.

**In progress:** Tool catalog (build plan: `docs/plans/TOOL_CATALOG_PLAN.md`) — a scalable tool
surface for jerv's growing tool count: DISCOVERY (compact menu) split from INVOCATION-SCHEMA
(on-demand `tool_guide`), umbrella dispatch tools for the source/action families. W1 (umbrellas —
jerv's surface 48 → 37 with no measured selection regression) shipped; W0a/W0b (metadata +
description trim) open; W2/W3 (the catalog machinery) gated behind a selection-accuracy eval.

**In progress:** Agent canvas (build plan: `docs/plans/AGENT_CANVAS_PLAN.md`) — jerv marks up and
cuts up images by tool call (`canvas`/`show_canvas`/`crop_regions`, YuNet faces, `render_html`).
W0–W5 and W7 shipped; W6 (on-box validation) open.

**In progress:** Candidate profile v2 (build plan: `docs/plans/CANDIDATE_PROFILE_V2_PLAN.md`) — a
leaner `candidate_profile` twin for a side-by-side A/B, with a deterministic public-records
pre-gather. C1 and C3 shipped; C2 (live on-box A/B + promotion decision) open.

**In progress:** Daily news v2 (build plan: `docs/plans/DAILY_NEWS_V2_PLAN.md`) — a deterministic-
gather → single-writer briefing engine beside the fan pipeline. V1 and V3 shipped — the engine won
the live A/B and was promoted to be `daily_news` (the pipeline path retired for this preset); V2
(the one measured triage/selection call) open.

**In progress:** News feed (build plan: `docs/plans/NEWS_FEED_PLAN.md`) — the curated `news_feed`
RSS/Atom source so briefing discovery stops depending on search engines that throttle a residential
IP. Waves A–B shipped (the tool + full-body pre-pull injection into the reader path); Wave C
(owner-editable feeds in PWA Settings + on-box cadence/threshold tuning) deferred, not scheduled.

**In progress:** JS-app fetch (build plan: `docs/plans/JS_APP_FETCH_PLAN.md`) — stop `web_fetch`
reading an un-rendered single-page app as a successful empty page: evidence-based `_looks_like_js_app`
detection, widened escalation, and an honest tool message. J1 shipped; J2 (live on-box validation +
threshold tuning) open.

**In progress:** Challenge solver (build plan: `docs/plans/CHALLENGE_SOLVER_PLAN.md`) —
challenge-interstitial detection plus the default-on Byparr stealth-browser fetch tier, with
`POST /api/debug/fetch` to verify the live path. S1 shipped; S2 (live on-box validation + tuning) open.

**In progress:** RapidOCR (build plan: `docs/plans/RAPIDOCR_PLAN.md`) — a deterministic CPU OCR
sidecar cross-validating the VLM extraction, exposed as the verbatim `ocr` tool to jerv and the
jcode sandbox. R0–R4 shipped; R5 (on-box sign-off against the live sidecar) open.

**In progress:** jcode grok internet (build plan: `docs/plans/JCODE_GROK_INTERNET_PLAN.md`) —
SearXNG-bridged `web-search`/`web-fetch` shell helpers for the sandbox CLIs plus the discovery
hook. S1–S5 shipped (#971, #981); E1 (raw-egress toggle) deferred on the shared-container caveat.

**In progress:** Report presets (build plan: `docs/plans/REPORT_PRESET_PLAN.md`) — P1 (checked-in
`{{variable}}`-parameterized report presets; the `candidate_profile` preset) shipped; P2 (batch
runs) and P3 (a compare-and-contrast preset from the library) open.

**In progress:** Deep-research scratchpad (build plan: `docs/plans/DEEP_RESEARCH_SCRATCHPAD_PLAN.md`)
— an in-memory, run-scoped findings ledger with an explicit visibility model for `deep_research`.
P1 + P1.5 landed; P2 (scope-model unlocks) deferred until a comparison mode needs it.

**Parked:** jcode session isolation (build plan: `docs/plans/JCODE_SESSION_ISOLATION_PLAN.md`) —
per-session network namespace; parked after the P1 spike (the P0 substrate reverted), kept for a
future revisit.
