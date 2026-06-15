# JBrain2 — Workflow Engine + Eval Harness (Phase 5) Implementation Plan

The buildable plan for Phase 5: generalize today's hardcoded ingest pipeline and
ad-hoc boot self-heals into a **data-defined workflow engine**
(`events → triggers → pipelines → actions → runs`) with a **scheduler** and a
**run-log UI**, stand up the **eval/benchmark harness** as the gating dependency
for the later self-improvement loops, and lay reversible **`skills`** groundwork.
Grounded in the current codebase (Phases 0–4 shipped: the Postgres job queue, the
single worker loop, the agent run-log, the eval `cases/` + promotion gate, the
settings store, the proposals/review surface). Every PR carries the project
non-negotiables (adapter-only LLM, storage abstraction, RLS-scoped sessions + an
isolation test per new table, tests-with-code at 80% / security-100%, Conventional
Commits + PR + CI green, `scripts/dev-setup.sh` updated with any new dep/tool/step).

> **Design stance: layer, don't rip out.** The existing `app.jobs` queue
> (SKIP-LOCKED claim, exponential backoff, stale-job reaper, `PermanentJobError`)
> already *is* the execution substrate and stays the executor. The engine is a
> **definition + dispatch + logging layer above it**: actions are the existing
> handlers described as data, a pipeline is an ordered set of actions, a trigger
> binds an event or a schedule to a pipeline, and a run is the audit trace. Per
> ROADMAP.md: "the actions are built first (they work as enqueued jobs today);
> this phase only gives them their scheduled and manual triggers." Nothing about
> the firewall, the claim loop, or backoff changes.

---

## 1. What exists today (the substrate this builds on)

- **Queue** (`queue.py`): `app.jobs` (`kind`, `payload` jsonb, `status`,
  `attempts`/`max_attempts`, `run_after`, `locked_at`, `last_error`). `claim()`
  is atomic `FOR UPDATE SKIP LOCKED` with a 10-min stale-lock reaper; `fail()` is
  `2^n`-minute backoff capped at 1h, with `permanent=True` short-circuit. Owner-only
  RLS (migration 0003). `enqueue` + `has_active(...)` dedup.
- **Worker** (`worker.py`): a single loop — `claim → handlers[kind] → complete/fail` —
  a 60s heartbeat, and a fixed **handler dispatch dict** for six kinds:
  `ingest_note`, `embed_note`, `integrate_note`, `ocr_attachment`,
  `consolidate_predicates`, `sync_predicates`.
- **Periodic work is boot-only self-heal today** — `backfill_pending_notes`,
  `backfill_unembedded_notes`, `backfill_pending_integration` (bounded 100,
  oldest-first by `created_at`), `purge.backfill_deleted_note_artifacts`,
  `backfill_consolidate`, `backfill_sync_predicates`. **There is no scheduler and
  no recurring timer.** Other triggers are hardcoded enqueues (note create → ingest
  in `api/notes.py`; ingest done → integrate in `ingest/pipeline.py`; merge /
  new-predicate resolution → consolidate in `analysis/repo.py`).
- **Run logging exists for the agent only**: `agent_runs`/`agent_steps`
  (`models/agent.py`, migration 0016) written via `agent/runlog.py`
  (`AgentRunLog.start/step/finish/bound`). The **Integrator logs to structlog only**
  — no `integration_run` table yet (`analysis/trace.build_trace` builds but never
  persists).
- **Eval harness** (`backend/evals/`): `cases/*.json` fixtures, `run.py` (live
  model, opt-in via `scripts/prompt-eval.sh`, never CI), `audit.py` (offline,
  CI-enforced), and a **pure promotion gate** (`promotion.py`:
  `promotion_decision(baseline: EvalRun, candidate: EvalRun, *, new_case: str)` —
  no task regression + no safety regression + the named new-case fixture passes).
  Each fixture carries a **two-dimensional** `FixtureScore(fixture, task, safety)`
  (the gate's whole point: a promotion can't buy task points with safety points).
  **Eval runs are not stored** anywhere — they print to stdout.
- **Settings/flags** (`settings_store.py`): `app.settings` key→jsonb, owner-only
  RLS, read live (no cache), typed getters; the established way to gate new
  behavior (e.g. `predicate_canonicalization`, `value_shape_enforce`).
- **API/Ops** (`api/`): owner-only routers, DI from `app.state`. `api/proposals.py`
  is the **tree-structured review surface** — the precedent for the run-log UI.
  `api/ops.py` is where an emergency-trigger control belongs.

---

## 2. Non-negotiables for the engine (binding)

These extend CLAUDE.md and `docs/ASSISTANT.md`'s invariants (I-#). Security-adjacent
ones are at 100% coverage.

- **E1. No confused deputy (I-8) — and this *inverts* today's model, so it is real
  work, not a no-op.** Today the worker runs **every** job under the all-domains
  `queue.SYSTEM_CTX` (`worker.py:45,52,60,68`; `queue.SYSTEM_CTX` "legitimately
  crosses every firewall"), and `enqueue` carries only row IDs — no triggering
  scope. E1 requires the engine to **carry the triggering `principal_id` + a
  fail-closed `domain` stamp** on the job/run and have the dispatcher build a
  **narrowed** `SessionContext` (`owner_scoped=True` + `domain_scopes`, per
  `db/session.py`) from it. Two explicit categories: **owner/agent-triggered
  actions narrow** to the trigger's scope; **system/scheduled actions and the
  legitimately-cross-domain ingest/integration pipelines** keep `SYSTEM_CTX`
  **but record that choice on the run** (the audit shows owner-system, not a
  smuggled escalation). The dispatcher never widens an owner/agent trigger's scope
  to satisfy a pipeline. *(This is a sequenced Track-A item, §5, not an assumed
  given.)*
- **E2. The engine never bypasses RLS or the firewall.** Events carry a fail-closed
  `domain` stamp (most-restrictive scope the triggering content touched); a trigger
  may not fan an event into a pipeline that writes a different domain. Per-table RLS
  isolation tests for every new table (`events`, `triggers`, `pipelines`, `actions`,
  `runs`, `run_steps`, `schedules`, `eval_runs`, `skills`).
- **E3. Definitions are data, code is the executor.** An `action` names an existing
  registered handler; the engine **cannot invent a handler** or call arbitrary code.
  The action registry validates at startup (unknown handler → boot failure), exactly
  like the `.tool`/`.prompt` registries. Pipeline/trigger rows reference actions by
  name+version only.
- **E4. Idempotent + reversible.** Re-running a pipeline or firing an emergency
  trigger is safe: actions keep the existing `has_active` dedup and write-once
  semantics; a manual run never produces a partial destructive write (the
  complete-turn-only rule the arbiter already enforces). Every migration is
  reversible.
- **E5. Bounded self-improvement spend (I-10) + kill-switch.** Any pipeline that
  makes LLM calls (eval runs, future distillation) is metered against a **separate
  daily self-improvement budget** with a global kill-switch setting, batched, and
  **never triggered by untrusted-origin content**.
- **E6. Untrusted-origin ordering (N14) — inert today; wire the hook, defer the
  teeth.** N14 is **moot in Phase 5**: every note is owner-authored
  (`queue.py:380-382` says so), and `notes.provenance` is `('human','agent')`
  (`models/notes.py`, migration 0018) — owner-typed vs agent-authored, **not**
  trusted vs untrusted capture. So this phase adds the `ORDER BY` ranking hook +
  its unit test to the integration trigger, but the owner-ahead signal it sorts on
  only becomes meaningful when an **untrusted-origin** producer exists (Phase 7
  guided-intake / OwnTracks). Defining that marker is Phase 7, not here; the plan
  must not sort "owner-first" on the existing two-value column and call it N14.
- **E7. Non-breaking cutover (mirror INTEGRATOR_PLAN discipline) — two distinct
  halves.** (a) **Dispatcher enqueue-equivalence is shadow-diffable:** the
  hardcoded triggers (note→ingest in `api/notes.py`, ingest→integrate in
  `ingest/pipeline.py`, resolution→consolidate in `analysis/repo.py`, the boot
  backfills) are all `enqueue` calls, so the dispatcher computes the same enqueues
  and diffs them before owning the path; the hardcoded trigger is removed only when
  the engine reproduces it exactly under test. (b) **Integrator run/pin persistence
  is net-new, not diffable:** the turn-loop logs to structlog only today
  (`build_trace` is built then embedded in a log payload, never persisted), so its
  `run` + `resolution_pin` writes have **no shadow baseline** — they ship behind
  their own setting and are validated by **convergence** (the harness corpus
  producing stable pins on re-run), not by diff-against-old.
- **E8. The classifier/boundary stay immutable to self-edit (I-12).** Domain stamping
  and the data/instruction boundary are code, not pipeline-editable data.

---

## 3. Data model (new tables — each `domain_id` where applicable + RLS isolation test)

| Table | Key columns | Notes |
|---|---|---|
| `actions` | `name` PK, `version`, `handler`, `params_schema` jsonb, `domain_optional` bool, `mutating` bool, `cost_class`, `dedup_key_expr` | The existing six handlers described as data; registry-validated at boot (E3). Reference data (global-read), owner/system write — the `canonical_predicates` RLS precedent. |
| `pipelines` | `name` PK, `version`, `steps` jsonb (ordered action refs + static params), `description` | Stored definitions; ingest + integration become two of these (E7). Linear first; DAG deferred (§7). |
| `events` | `id`, `type`, `payload` jsonb, `domain_id`, `principal_id`, `occurred_at`, `dispatched_at` | Append-only event log; `domain_id` fail-closed (E2). |
| `triggers` | `id`, `on_event` \| `on_schedule_id`, `pipeline`, `filter` jsonb, `enabled`, `manual` bool | Binds an event type or a schedule to a pipeline; `manual=true` marks an emergency-fireable sweep. |
| `schedules` | `id`, `cron`, `timezone`, `next_run_at`, `last_run_at`, `enabled` | The scheduler's claim targets (SKIP LOCKED on `next_run_at`); owner timezone reuses `owner_timezone`. |
| `runs` | `id`, `kind` (`agent`/`integration`/`pipeline`), `pipeline`?, `trigger_id`?, `session_id`? (nullable), `prompt_version`? (nullable), `ran_as` (`scoped`/`system`), `status`, `stop_reason`, `step_count`, `cost_tokens`, `domain_id`, `principal_id`, `started_at`, `ended_at` | **Generalizes `agent_runs` by migrating it** (see note below): `session_id`/`prompt_version` relax to nullable under a CHECK `kind='agent' ⇒ both NOT NULL` so the agent invariant survives; `ran_as` records E1's scope choice. |
| `run_steps` | `run_id`, `idx`, `kind`, `name`, `ok`, `cost_tokens`, `tool_version`?, `job_id`? | Generalizes `agent_steps`; `job_id` is a **nullable uuid FK → `app.jobs.id` `ON DELETE SET NULL`** so job aging-out never breaks run-log reads (N2). |
| `resolution_pin` *(persists `analysis/pins.py`)* | PK `(note_id, chunk_id, occurrence_index, decision_kind)`, `entity_id`?/`normalized_predicate`?, `surface`, `span_text_hash` | Storage for the **existing pure** `ResolutionPin` (pins.py) — migration adds persistence only. `chunk_id` is **in the key**: `occurrence_index` is chunk-relative, so a note-only key collides across chunks (the pins.py docstring's explicit warning). Cascade-purged with the note. |
| `eval_runs` | `id`, `suite`, `version_label`, `model`, `new_case`?, `scores` jsonb (per-fixture `{fixture, task, safety}`), `created_at` | Stored results so `promotion_decision` reconstructs `EvalRun`/`FixtureScore` candidate↔baseline **without losing the task/safety split** (a flat blob would defeat the gate). |
| *(groundwork)* `skills` | `name`, `version`, `status` (shadow/active/quarantined), `domain_id`, `body`, `description`, `embedding`, `success_stats` | Reversible Alembic + `skill_version` stamped on `runs`; **no promotion logic this phase** (that's Phase 6, I-5/I-6). |

`agent_runs`/`agent_steps` are **migrated in place, not duplicated**: rename into
`runs`/`run_steps`, backfill `kind='agent'`, relax `session_id`/`prompt_version` to
nullable under the `kind='agent'` CHECK above, and **repoint the dependent FKs in the
same migration** — `agent_episodes` and `agent_turns` FK `agent_runs.id`
(`agent_episode_refs` reaches it transitively via `agent_episodes`, so it needs no
direct repoint). The "typed view over `agent_runs`" option is
**rejected**: a view can't both keep the existing non-null agent FK and admit
session-less integration/pipeline rows. The existing agent run/step tests are the
regression gate for the rename.

---

## 4. Carried-forward items from Phases 3–4 (independent; can land first)

Three of these are small and don't depend on the engine — they're good Wave-0
parallel work that also de-risks the schema:

- **`extraction_truncated` review card.** `plan_to_extraction` rebuilds the
  `Extraction` with `dropped_facts=0`, so the per-note fact cap fires but no card is
  filed. Thread the real dropped count through the intent→plan→extraction adapter and
  file the card. Self-contained; a regression test that an over-cap note surfaces the
  card. *(docs/archive/CUTOVER_V1_REMOVAL.md, docs/archive/INTEGRATOR_PLAN.md.)*
- **N14 ordering hook (inert until Phase 7).** Add the owner-ahead `ORDER BY` hook to
  `backfill_pending_integration` (today: oldest-first by `created_at`) + a unit test,
  but **flag it a no-op in Phase 5**: `notes.provenance` is `human`/`agent` (both
  owner-trusted), not an untrusted-origin marker, so there's nothing to rank against
  yet. The real teeth land with the untrusted producer in Phase 7; do **not** sort
  "owner-first" on the existing two-value column and call it done.
- **Agent-loop maturation.** Auto-wire `reflexion` into the default turn (gated, retry
  only on strict verifier improvement, cap N=2 — the module is pure today and never
  imported by `loop.py`); yield the `JobEnqueuedEvent` that already exists in
  `agent/contracts.py` but is never emitted; and **confirm every registered `.tool`
  has its `(name, version, digest)` pinned** like `note_extract` — the digest-pin
  mechanism already exists (`toolfile.py` + the read-tool tests), so this closes any
  unpinned-tool gap, **not** a new CI step (the `.prompt` analogue is itself a pytest
  digest pin, not CI YAML). Each is an isolated PR with its own test.

---

## 5. The waves (parallel tracks; each ends with a review gate, §6)

### Wave 0 — Foundation & contracts (small; lands first; unblocks everyone)
- **W0.1 — Action registry.** Wrap the six existing handlers as registered `actions`
  (data + a registry that validates handler existence at boot, E3). Behavior is
  unchanged for known kinds; the one deliberate change is that an **unknown/missing
  handler now fails at boot** (like the schema registry) rather than failing the job
  at run time (`worker.py:48-54`). Migration + RLS test for `actions`.
- **W0.2 — Migration DDL + typed contracts** for `events`/`triggers`/`pipelines`/
  `schedules`/`runs`/`run_steps`/`eval_runs`/`resolution_pin`/`skills` — each with its
  RLS policy + an isolation-test stub, and the Pydantic/dataclass shapes the tracks
  build against (event, trigger filter, pipeline step, run/step records).
- **W0.3 — Carried-forward independent fixes** (§4): the `extraction_truncated` card,
  N14 ordering, and the agent-loop maturation trio. Parallel, no engine dependency.

### Wave 1 — four concurrent tracks

| Track | Owns | Builds independently | Integrates |
|---|---|---|---|
| **A — Engine core** (critical path) | the in-place `agent_runs`→`runs` rename incl. nullable `session_id`/`prompt_version` under the `kind='agent'` CHECK + repointed dependent FKs (B1); the Integrator's run + `resolution_pin` persistence (net-new, §E7b, on `analysis/pins.py`); the **E1 scope carrier** — a triggering-principal + fail-closed domain stamp on the job/run and a dispatcher that builds a **narrowed** `SessionContext` (vs `SYSTEM_CTX` for system/cross-domain actions); and the **event→trigger→pipeline→action dispatcher** behind a setting, **shadow-diffed** for enqueue-equivalence (§E7a). | the dispatcher + run-log writer against the fake queue/LLM; the agent + Integrator keep current behavior, now logging through `runs`. | B's scheduler and D's UI read its runs. |
| **B — Scheduler & task migration** | the **scheduler tick** (claim `schedules` by `next_run_at` SKIP LOCKED, enqueue the bound pipeline; advance `next_run_at` **app-side** so a fake clock controls it, N3) + schedule rows; migrate the boot self-heals + `consolidate_predicates`/`sync_predicates`/purge onto **startup + scheduled + manual** triggers; the **emergency-trigger** endpoint (`POST /ops/triggers/{id}/run`). **Summary re-embedding is a §7 spike, not a committed migration** (it's inline in the resolution read path today). | the scheduler + migrated sweeps against the injected clock — no engine-core needed for the sweeps to run as today. | actions/runs come from A; trigger button surfaces in D. |
| **C — Eval harness + budgets** | `eval_runs` storage + an `eval_run` **action** (opt-in, real model behind the budget); wire `promotion.py` to compare stored candidate↔baseline; the **self-improvement budget + kill-switch** setting (E5); reversible `skills` groundwork + `skill_version` on `runs`. | the gate + storage are pure/fixture-driven; the action is faked in CI. | runs/budget plumbing from A; nightly eval becomes a B schedule. |
| **D — Run-log UI** | `GET /runs` (+ `/runs/{id}` step tree) modeled on `api/proposals.py`; the **Ops "Runs" surface** (recent runs, status, drill into steps, failure `last_error`, re-run / emergency-trigger button) per `docs/DESIGN.md`. | against fixture run data + the contracts. | wires to live `/runs` when A lands. |

**Critical path:** W0 → A (runs unification + dispatcher shadow) → Wave-2 cutover.
B, C, and D overlap A.

### Wave 2 — Integration & cutover
- Flip **ingest** and **integration** to run as `pipeline` definitions: dispatcher
  in shadow → diff clean over the harness corpus → remove the hardcoded enqueues
  (E7). The Integrator turn-loop now persists a `run` + `resolution_pin`s.
- Migrate the boot self-heals fully onto **startup triggers**; the nightly sweeps
  onto **schedule triggers**; each remains **manually fireable** from Ops (no
  service restart to run a sweep).
- End-to-end fake-adapter + testcontainers tests: an event drives a pipeline to a
  logged run; a schedule fires a sweep; an emergency trigger runs one on demand; a
  failed step backs off and surfaces in the run log.

---

## 6. Review gates between waves (no wave skips its gate)

1. **Agent review pass** over the wave's diff: `/code-review` for correctness + reuse;
   for the security-touching waves (the dispatcher's scope handling, scheduler
   `SYSTEM_CTX` use, RLS on every new table, budget/kill-switch, the cutover) also a
   **red-team pass** + the `security-review` skill, checked against E1–E8 and I-8/I-10/I-12.
2. **CI gate:** lint, typecheck, tests green; 80% / security-100% coverage; the
   `.prompt`/`.tool` digest pins + the new action-registry's boot validation run
   **as part of the test suite** (they are pytest pins, not separate CI YAML);
   `dev-setup.sh` current.
3. **Human gate:** PR(s) reviewed + merged; open decisions (§7) resolved or carried.
4. **Iterate, then proceed:** the next wave fans out only once the gate is green; Wave 2
   gets its own end-to-end gate before any Phase-6 (wiki / skill-learning) work starts.

---

## 7. Open decisions (carried into the migration PRs)

- **`runs` vs `jobs`.** `runs` is the *definition/audit* layer; `app.jobs` stays the
  *executor*. Confirm `runs` does not absorb `jobs` (a run references the jobs its
  steps enqueued) — keeps the proven claim loop untouched.
- **Pipeline shape.** Linear ordered steps first; a DAG (fan-out/join) only if a real
  pipeline needs it — start linear, the ingest/integration pipelines are linear.
- **Cron representation.** A small cron subset vs an interval/`next_run_at` field.
  Lean interval + explicit `next_run_at` (no cron parser dep — zero-new-dep goal);
  nightly is `interval=1d at owner-local 02:00`.
- **Scheduler concurrency.** One worker today; design the `schedules` claim SKIP-LOCKED
  so a second worker is safe later, but don't build multi-worker coordination now.
- **`agent_runs` migration form** — *resolved* (§3): rename in place with `session_id`/
  `prompt_version` nullable under a `kind='agent'` CHECK and dependent FKs repointed;
  the typed-view option is rejected (can't admit session-less rows). The existing
  agent run/step tests are the regression gate.
- **Budget dollar values** for the self-improvement kill-switch (seed conservative;
  tune like the integration budget).
- **Summary re-embedding** currently lives inline in `analysis/entities.py` as a
  prerequisite — confirm it can be lifted to a standalone scheduled action without a
  read-time regression, or keep the inline path and only add the sweep.

## 8. Phase-5 exit

Ingest and at least one nightly sweep run as **data-defined pipeline definitions**;
every periodic/swept task is **on-demand triggerable** from Ops without a restart; a
failure is fully diagnosable from the run log alone; the agent and Integrator both log
to the unified `runs`; the eval harness stores runs and gates promotion behind a safety
regression term; and the new tables prove domain isolation. Skill *learning* and
prompt/tool *self-edit* (Loops 2/4) remain deferred to Phase 6 — this phase builds the
runway (runs, eval harness, budgets, `skills` schema), not the loops.
