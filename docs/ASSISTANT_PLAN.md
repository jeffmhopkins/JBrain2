# JBrain2 — Assistant Implementation Plan

The buildable plan for `docs/ASSISTANT.md`, grounded in the current codebase
(Phases 0–3 are implemented: auth, notes, ingest, search, analysis, RLS, the
Postgres job queue, `.prompt` loading, and an eval harness all exist). This plan
is **deep on the Phase-4 slice** (the next buildable work) and lighter on 5–7,
which depend on the workflow engine and wiki. Every PR carries the project
non-negotiables (adapter-only LLM, storage abstraction, RLS-scoped sessions +
isolation test per new table, tests-with-code at 80% / security 100%,
Conventional Commits + PR + CI green, `dev-setup.sh` updated with any new
dep/tool/step).

## Where the assistant code lives

A new `backend/src/jbrain/agent/` package, mirroring the existing
repo→service layering and `.prompt` sidecar conventions:

```
agent/
  loop.py            # the thin ReAct turn loop + guardrails
  tools/             # .tool sidecars + their handlers (compose existing services)
  toolregistry.py    # discovery, validation, schemas_for(scopes)
  memory.py          # Tier-A read/write/compaction (service over agent_memory/episodes)
  classifier.py      # write-time fail-closed domain classifier
  reflexion.py       # deterministic verifiers + optional critic
  proposals.py       # Proposal staging + dependency-safe enactment
  session.py         # agent-session capability (read scope + action policy)
  runlog.py          # step log → agent_runs (becomes workflow `runs` in P5)
  prompts/*.prompt   # system persona + tool-use policy, reflexion critic, classifier
connectors/          # the egress chokepoint: Connector protocol + registry + guard
  medical.py         # RxNorm/RxNav, MedlinePlus connectors
  geocode.py         # local-first geocoder client (P7)
models/agent.py      # SQLAlchemy models for the new tables
api/agent.py         # /chat (SSE/WS streaming)
api/sessions.py      # start/list agent sessions
api/proposals.py     # the review-inbox Proposals surface
```

Reuses verbatim: `llm/router.py` task profiles + `llm/fake.py`; `db/session.py`
`scoped_session`/`SessionContext` (already carries `domain_scopes`); `queue.py`
`enqueue` for deferred/long tools; `storage.py`; `notes/`, `search/`, `analysis/`
services as the tools' backends; `evals/` for the (later) promotion gates.

## Data model (new tables, all `domain_id` + RLS isolation test)

| Table | Key columns | Notes |
|---|---|---|
| `agent_sessions` | `principal_id`, `title`, `status` (active/ended), `domain_scopes[]`, `subject_ids[]`, `started_at`, `last_active_at` | The capability: the selected **read scope** that builds the session's `SessionContext` |
| `agent_runs` | `session_id`, `status`, `step_count`, `cost_tokens`, `cost_usd`, `stop_reason`, `tool_versions` jsonb, `prompt_version`, `started/ended_at` | One agent turn-loop execution; **becomes a workflow `runs` row in P5** |
| `agent_steps` | `run_id`, `idx`, `kind` (model/tool), `tool_name`, `tool_version`, `request`/`response` jsonb, `is_error`, `cost` | The audit/training trace; minimal answer text (purge-friendly) |
| `agent_memory` | `principal_id`, `subject_id`, `domain_id`, `block_kind` (core/task/self_semantic), `body_md`, `revision`, `superseded_by`, `source` (owner_confirmed/…) | MD "files" as rows; ACE delta-edit history; behavioral tier is **owner-confirmed-write only** |
| `agent_episodes` | `session_id`, `run_id`, `domain_id`, `body`, `importance`, `last_accessed_at` + segregated-namespace embedding | Episodic trace; **fail-closed** domain stamp; never citable |
| `agent_episode_refs` | `episode_id`, `fact_id`/`entity_id`/`note_id` | Pointers-not-copies (A-MEM links); purge cascade target |
| `proposals` | `session_id`, `principal_id`, `kind`, `status` (staged/approved/enacted/rejected/expired), `provenance` jsonb, `domain_id`, `subject_id` | The stage-and-approve unit |
| `proposal_nodes` | `proposal_id`, `parent_id`, `type` (group/leaf), `op`, `label`, `preview` jsonb, `deps[]`, `status` (pending/approved/rejected) | The tree; **dependency-safe partial approval** |
| *(altered)* `notes` | + `provenance` (human/agent), `source_ref` | Agent-authored notes; **normal extraction weight** |
| *(P6)* `skills` | `name`, `version`, `status` (shadow/active/quarantined), `domain_id`, `body`, `description`, embedding, `success_stats` | Procedural memory; read-only auto-promote, mutating owner-gated |
| `connector_cache` | `connector`, `input_hash`, `result` jsonb, `domain_id`, `fetched_at`, `ttl` | Cached external reference data; `domain_id` (location cache is location-scoped) + RLS |
| `connector_log` | `connector`, `input_hash`, `domain_id`, `principal_id`, `at` | Egress audit trail (no payload, hash only) |

Discriminator/`block_kind`/namespace columns are RLS-eligible so a single-scope
session cannot read a multi-scope episode or another domain's memory.

## Phase 4 — the buildable slice (sequenced PRs)

Dependency order; each is one PR with tests.

**P4.1 — Adapter tool-calling (foundational).** Extend the adapter for native
tool use: add `ToolDef`/`ToolUse`/`ToolResult` + `stop_reason` to `llm/types.py`,
a `tools=` parameter and tool-block handling to `LlmClient.complete` (or a
sibling `complete_tools`), implemented in `anthropic.py`, `openai_compat.py`, and
`fake.py` (scripted `tool_use` blocks for deterministic loop tests). No new
runtime dep. *Gate:* fake-driven multi-turn tool round-trip; both provider
adapters covered.

**P4.2 — `.tool` sidecars + registry.** Define the sidecar (YAML frontmatter:
`name`, `version`, `params` JSON-schema, `domains`, `mutating`, `side_effecting`,
`cost_class`, `response_format` — text and/or a `view`; prose body = the model-facing
description). Loader beside `llm/promptfile.py`; `ToolRegistry` discovers/validates
at startup (invalid sidecar or missing handler → startup failure) and exposes
`schemas_for(scopes)`. **CI version-bump guard** mirroring the `.prompt` guard.
Ship the **tool-view contract**: a `ViewPayload` schema (one registered component
name + data-only typed slots + `surface` hint), the shared `CitationRef`
pointer-not-copy types (`fact`/`entity`/`note` id + label), and a first-party
**component registry** (`frontend/src/agent/views/`). **MVP set (7, on 3
composable primitives — see `docs/research/self-improving-agent/G-tool-view-
components.md`):** primitives `data_table`, `stat_block`, `citation_card`; reads
`lab_plot`; interactives `record_list` (→ `list_*` tools, staged),
`appointment_card` (→ `manage_appointment` Proposal + ICS subscribe), `confirm_panel`
(→ approve/reject a Proposal node). Standard tier (soon): `entity_card`, `timeline`,
`wiki_preview` (P6), `med_card`, `txn_table` (collapse into `data_table` unless a
`money` cell is insufficient). A `view` is validated against the named component's
schema server-side; **one view = one component** (no trees), components express
`tone`/`flag` enums never colors, and **no model-authored markup ever renders**
(`docs/DESIGN.md` "Agent tool views", invariants #1/#9). *Gate:* sidecar-validity
unit tests; guard fails on unbumped prose change; a `view` failing its component
schema is rejected, not rendered.

**P4.3 — Agent session capability.** `agent_sessions` table + migration + RLS
test. `agent/session.py` turns a selected (domain_scopes, subject_ids) into a
`SessionContext` (narrower than the owner's all-scopes default). Per-tool
**permission class → policy** (`read` direct within scope, `mutate`/`sensitive`
staged, `external` denied). `api/sessions.py` start/list. *Gate:* RLS isolation
(a health-only session cannot read finance); policy denies an out-of-scope write.

**P4.4 — The thin loop + read-only tools.** `agent/loop.py`: turn structure,
tool dispatch under `scoped_session`, hard guardrails (`max_steps`,
`max_cost` from the task profile, `wall_clock_timeout`,
`max_consecutive_tool_errors`, per-session `tool_allowlist`), structured tool
errors. Step log → `agent_runs`/`agent_steps`. First tools (compose existing
services): `search` (hybrid), `read_note`, `read_entity`, `read_fact`. System
`.prompt` (persona + tool-use policy + the **data/instruction boundary**, I-1).
*Gate:* fake-adapter loop tests (multi-turn, guardrail trips, error self-repair);
per-tool RLS isolation.

> **Tool development path (text-first → view-later).** Every tool ships
> **text-only first**: the handler returns a concise text observation, which is
> all the model needs and all the loop streams. A tool gains a rich **`view`**
> (a registered first-party component — e.g. `read_entity` → the `entity_card`
> with its schema.org kind badge, facts-as-edges, and "open entity" link) **later,
> additively, when the view registry lands (P4.5/P4.4-frontend)** — without
> changing the tool's contract, since `response_format` already carries an
> optional `view` beside the text. So `read_entity`/`read_fact` (the P4.4c
> follow-up) return text now and grow a card later; the same path applies to
> `lab_plot`, `record_list`, etc. Build the tool, then dress it.

**P4.5 — Chat API + streaming + Full Brain PWA surface.** `api/agent.py` `/chat`
emitting SSE/WS events (`text_delta`, `tool_call`, `tool_result`, `tool_view`,
`job_enqueued`, `done`); resume from the persisted run. Frontend: the Full Brain
conversation surface + the lateral swipe (Sessions/Proposals) per `DESIGN.md`
(swipe-right → Sessions, swipe-left → Proposals; no edge chrome); the chat renderer
maps `tool_view` payloads to the component registry (inline, or into the shared
`<Sheet>`/`<Dialog>`). *Gate:* httpx streaming test; Vitest for the surface and a
registry render test (unknown component → nothing); the existing mocks are the spec.

**P4.6 — Tier-A memory + domain classifier.** `agent_memory`,
`agent_episodes`, `agent_episode_refs` + migrations + RLS tests (incl. the
multi-scope-episode isolation test). `agent/memory.py` (read/recall via existing
RRF in a **segregated namespace**; ACE delta-edit writes). `agent/classifier.py`:
**fail-closed** write-time domain stamping (episodic = most-restrictive scope
touched; behavioral = ANALYSIS.md asymmetric rule, owner-confirmed only). Tools:
`recall`, `memory.read`, `memory.edit`, and `remember` (**owner-confirmed only**,
I-2). Memory read **as data, never instruction** (I-3). Note-deletion **purge
cascade** to episodes + the assertion test (I-11). *Gate:* RLS isolation; purge
test; classifier fail-closed unit tests.

**P4.7 — Reflexion (Loop 1).** `agent/reflexion.py`: **deterministic** verifiers
(cited facts exist & in-scope; claims ground in retrieved chunks; mutations
validate against schema) + optional cheap critic as tiebreaker; retry **only if
the verifier score strictly improves**, hard cap N=2. Fully ephemeral. *Gate:*
pure unit tests; no persistence touched.

**P4.8 — Proposal primitive + review inbox.** `proposals`/`proposal_nodes` +
migrations + RLS tests; `notes.provenance` migration. `agent/proposals.py`:
staging + **dependency-safe enactment** (enact a leaf only when every prereq is
approved; approved-but-unmet-prereq → held). Kinds shipped now: `agent-correction`
and `knowledge` (both re-enter as **normal-weight, source-attributed,
provenance-flagged** agent notes through the existing extraction pipeline, I-7).
The `propose_correction` tool stages a Proposal instead of writing. `api/proposals.py`
+ the Proposals page (the unified review-inbox view, focused on agent proposals).
*Gate:* cascade + dependency-hold + RLS tests; enactment touches only
approved+satisfied leaves.

**P4.9 — External connectors (egress chokepoint + medical/medicine).** A new
`backend/src/jbrain/connectors/` package: the `Connector` protocol (pinned base URL
from config, typed request schema, response parser, cache policy, rate limit,
`domain`, consent flag), a registry, and the **egress guard** (typed params only;
reject anything beyond the declared shape). Server-side only (api/worker), reusing
the existing `httpx`. New tables `connector_cache` (connector + normalized-input
hash → parsed result + TTL; `domain_id` + RLS) and `connector_log` (audit). First
connectors as `external`-class tools whose **use is proposed**: an off-box call
**stages an `egress` Proposal** (new kind, builds on P4.8) whose **preview is the
exact outbound payload**, and the call runs only on approval — standing per-connector
approval is an optional owner widening, any connector is disable-able.
`lookup_medication` (NLM RxNorm/RxNav + openFDA), `lookup_condition` (MedlinePlus
Connect / Clinical Tables). Results are **data wrapped in the I-1 boundary**,
source-attributed, never minted as facts without a Proposal. *Gate:* egress-guard
unit tests (a param carrying extra/owner data is rejected); the egress Proposal is
required before any network call fires (test: no approval → no egress); cache
hit/miss; the connector is faked in tests (no live network, like the LLM adapter);
RLS test on `connector_cache`. **No new runtime dep** (`httpx` exists);
`dev-setup.sh` unaffected.

**Phase-4 exit:** Full Brain chat is the default way to ask "what do I know about
X," runs are logged, the agent never writes citable truth directly, every staged
change is owner-approved through a Proposal tree, and the three new RLS tables
prove domain isolation. (`wiki-restructure`, skill learning, and prompt
self-editing are explicitly deferred — see below.)

## Phase 4 build — parallel tracks

The Phase-4 PRs are sequenced by dependency but **fan out into ~4 concurrent
workstreams** after one small foundation. Three accelerators make this possible and
already exist: the **fake LLM adapter** (no one waits on real models), the **mocks**
(`docs/mocks/assistant-*.html` are the frontend's spec today), and **independent
migrations** (each new table is its own RLS-tested PR).

**Branch strategy.** Merge this design branch to `main` first (one docs PR); every
track/PR below is a short-lived feature branch off `main`, branch + PR + CI green
(CLAUDE.md). Parallel work runs in isolated worktrees, one branch per PR.

**Wave 0 — foundation (lands first; small, unblocks everyone):**
- **P4.1 adapter tool-calling** — `ToolDef`/`ToolUse`/`ToolResult`/`stop_reason`,
  `tools=` on `complete`, fake scripting. Everyone then tests against the fake.
- **Contracts PR** — the shared shapes others build against, in one small PR: the
  streaming event schema (`text_delta`/`tool_call`/`tool_result`/`tool_view`/
  `job_enqueued`/`done`); the `ViewPayload` + `CitationRef` types and the
  permission-class enum (`read`/`mutate`/`external`/`sensitive`) → policy mapping;
  the `.tool` sidecar frontmatter schema; and the **migration set DDL** for every
  new table, each with its RLS policy + an isolation-test stub.

**Then four tracks run concurrently:**

| Track | Owns (PRs) | Builds independently of the loop | Integrates |
|---|---|---|---|
| **A — loop / critical path** | P4.4 loop + read-only tools → P4.5 `/chat` streaming backend | the turn loop, guardrails, run-log, dispatch over `scoped_session` | the spine B/C/D wire into |
| **B — schema & security** | P4.3 sessions · P4.6 memory + classifier · P4.8 proposals · P4.9 connector cache + egress guard + medical | migrations, repositories, the fail-closed classifier, tree cascade / dependency-safe enact, egress guard, all RLS tests | tools plug into A; egress kind needs P4.8 |
| **C — tooling substrate** | P4.2 registry + CI guard · P4.7 reflexion verifiers | sidecar loader/registry, version guard, pure deterministic verifiers | registry feeds A; reflexion wraps A's turns |
| **D — frontend** | P4.5 UI + the view registry | Full Brain surface, lateral swipe, transcript + `tool_view` renderer, view components, Sessions + Proposals pages — against the mocks + contracts | wires to live `/chat` when A lands |

**Wave 2 — integration:** plug B's tools (recall/`remember`, `propose_correction`,
connectors) and C's reflexion into A's loop; connect D to the live stream; the
egress-Proposal path (P4.9 × P4.8); end-to-end fake-adapter + testcontainers tests.

**Critical path** = P4.1 → P4.4 → P4.5-backend → Wave-2 integration; all of B, C, and
D overlap it. So after a one-PR foundation, roughly four people/agents can work at
once without blocking each other.

## Phase 5 — workflow engine alignment

- `agent_runs`/`agent_steps` **become** workflow `runs` (the loop emits the same
  events); the self-improvement loops become scheduled **pipeline defs** with
  per-principal + global **daily cost budgets** and a kill-switch (I-10).
- Stand up the **eval/benchmark harness** on the existing `evals/` (held-out
  fixtures, a baseline, and a curator for "the originating task class") — this is
  the **gating dependency** for Loops 2 and 4, so it lands here.
- `skills` schema groundwork (Alembic, reversible); `skill_version` stamped on
  `runs` for auditability (mirrors the `.prompt` version stamp).

## Phase 6 — wiki-era loops

- **Skill learning (Loop 2):** distill verified runs into `skills`; **read-only
  compositions auto-promote** on a replay eval that **includes a safety/
  groundedness regression** (not task-success alone); **mutating/cross-domain
  skills are owner-gated** (I-5/I-6); active-skill cap + decay eviction; sanitized
  descriptions (data, not instruction).
- **Prompt/tool self-edit (Loop 4):** the meta-pass drafts a `.prompt`/`.tool`
  **diff + version bump + new eval fixture** as a **branch + PR** (or a review-inbox
  item that opens one); security-relevant edits must pass an **adversarial-injection
  suite at 100%**; the data/instruction-boundary and classifier prompts are
  **immutable to self-edit** (I-12).
- **Tier-B durable knowledge** fully closes through the wiki's correction-note
  machinery; the **`wiki-restructure` Proposal kind** ships — the agent stages a
  tree of split/merge/retitle/recluster/rewrite ops and the **machine wiki builder
  enacts the approved, prereq-satisfied leaves** as revisions (agent never writes
  prose).

## Phase 7 — outer-ring principals

- Intake-link / device-key principals get a **default-deny, capture-only** tool
  allowlist (I-8) and **cannot write agent memory/skills or trigger
  self-improvement jobs**; agent-internal jobs run at the **triggering principal's
  scope** (no confused deputy).
- **Geocoding connectors** (`geocode_reverse`, `geocode_forward`) land with the
  location domain, served by a **self-hosted geocoder container** (Nominatim/Photon
  + a regional OSM extract, mirroring the `embed` container) so location data
  **never leaves the box**; an external geocoder is a consented, logged fallback
  only. New compose service + a `dev-setup.sh`/install step (the OSM extract is the
  one real infra addition — gate it behind the location domain so the base install
  stays lean). `connector_cache` for geocoding is `location`-scoped with the same
  RLS test. Enables the deferred `place_card` (resolved address as text — no tiles).

## Open questions — resolved here

- **Eval harness** (was open): built in Phase 5 on `evals/`; fixtures are curated
  per task class from real runs the owner accepted/rejected; baseline = current
  prompt/skill version's score; promotion requires *no regression on the existing
  set + a win on the new case*, with a safety-regression term.
- **Compaction vs decay:** **two mechanisms, one table.** Session compaction is
  synchronous, triggered by the loop near the context limit (summarize-and-pointer
  the oldest episodes of the *current* run); nightly decay is a batched job over
  `agent_episodes` (importance/recency pruning). Both write through `agent/memory.py`.
- **Importance scoring:** heuristic-first (owner-corrected? tool error?
  owner-confirmed "remember"?) — **no per-episode LLM call**; content-derived
  signals are untrusted and capped (I-11/A11). An LLM poignancy score is a deferred
  option behind a cheap task profile.
- **Cost ceiling:** per-task `max_cost` (exists) + a per-session interactive
  budget + a **separate daily self-improvement budget** with a kill-switch
  (Phase 5).
- **Combined ER:** the table block above is the authoritative sketch; the
  migration PRs draw the FKs.

## Execution cadence (review gates between waves)

**Every wave/phase ends with a critical-review-and-iteration gate before the next
begins** — mirroring the researcher → reviewer → red-team discipline that produced
this design. No wave skips its review because it "looks small."

1. **Agent review pass** over the wave's diff: `/code-review` for correctness and
   reuse; for any security-touching wave (RLS, the classifier, the
   data/instruction boundary, Proposal enactment, the egress chokepoint) also a
   **red-team pass** and the `security-review` skill, checked explicitly against
   the relevant invariants (I-1..I-12, the per-loop autonomy boundaries, the
   egress-Proposal gating).
2. **CI gate:** lint, typecheck, tests green; 80% / security-100% coverage;
   `.prompt`/`.tool` version guards; `dev-setup.sh` current.
3. **Human gate:** the wave's PR(s) reviewed and merged; open questions resolved or
   explicitly carried forward into the plan.
4. **Iterate, then proceed:** findings are addressed on the same branch; the next
   wave fans out only once the gate is green. Wave 2 (integration) gets its own
   end-to-end gate before any Phase-5 work starts.

## Cross-cutting obligations (every PR)

RLS isolation test for any new table; LLM calls faked in tests (the eval suite is
the deliberate, out-of-CI quality gate); `dev-setup.sh` updated for any new
dep/tool/step (**goal: zero new runtime deps** — the loop is stdlib + existing
stack); `.prompt`/`.tool` version-bump guards; security-adjacent paths
(classifier, RLS scoping, the data/instruction boundary, Proposal enactment) at
**100%** coverage.

## Suggested starting point

P4.1 (adapter tool-calling) unblocks everything and is self-contained — it is the
right first PR. P4.2–P4.4 then stand up a working read-only agent over the phone
before any memory or staging exists, which is the smallest end-to-end daily-usable
increment.
