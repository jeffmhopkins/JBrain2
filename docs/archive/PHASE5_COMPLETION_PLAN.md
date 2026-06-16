# JBrain2 — Phase 5 Completion Build Plan (residual slices + Phase-6 deferrals)

> **✅ COMPLETED — Phase 5 is closed (2026-06).** All residual tracks shipped
> (R, H·A, H·B, H·C, S, N, D), migrations run through **0044**, and the one open
> owner decision is resolved: **Loop-4 prompt/tool self-edit is deferred to Phase 6**
> (§5.2) — carried forward in `ROADMAP.md` Phase 6. This doc is now a completed
> build record (archived under `docs/archive/`); the next frontier is Phase 6 (Wiki).

The buildable plan for **finishing Phase 5**. The workflow engine (events →
triggers → pipelines → actions → runs), the scheduler, the run-log, and the
non-breaking cutover **already shipped** — PRs #217 / #220 / #221, migrations
**0035–0041**, the `agent_runs`→`runs` unification, the dispatcher in LIVE mode,
the nightly + 5-minute reconciler schedules, and the emergency-trigger Ops
control are all in `main`. The eval-harness *gate* shipped too (the pure
`promotion_decision`, `EvalRunStore`, `PromotionService`, `SelfImprovementGate`,
and an opt-in `eval_run` action) — and is now **fed by a nightly `eval_run`
schedule** (Track H·B, migration 0044).

This document is the residual-completion plan: the small, independent slices that
land Phase 5 as *done*, the one substantive new track (reflexion-in-the-live-turn
and harness completion), and an explicit record of what is **deliberately deferred
to Phase 6** with rationale. It is a plan doc only — no implementation code lands
from this file. It follows the format of `docs/archive/WORKFLOW_ENGINE_PLAN.md` (which it
supersedes and which is now archived — see Track D).

> **Design stance: the engine is done; finish the wiring, then close the books.**
> Every track here is either a near-mechanical mirror of something already shipped
> (the reconciler sweep mirrors 0041; the nits are local), a self-contained design
> slice that writes nothing durable (reflexion is Loop 1, ephemeral), or pure doc
> hygiene. The two substantive tracks — **R** (reflexion in the live turn) and
> **H** (eval-harness completion A–C) — are the only ones that need real design
> attention, and even those reuse fully-shipped infrastructure. The big
> self-improvement machinery (skill learning, durable-knowledge promotion,
> predicate-canon self-improvement, prompt/tool self-edit) is **out of scope here**
> and lands in Phase 6, because each needs a spine this phase deliberately did not
> build (a wiki/correction-note loop, a skills consumer, an adversarial suite).

Every PR carries the project non-negotiables (CLAUDE.md): adapter-only LLM,
storage abstraction, RLS-scoped sessions + an isolation test per new table,
tests-with-code at **80% backend coverage / security paths 100%**, real Postgres
via **testcontainers** with **LLM calls faked**, Conventional Commits + branch +
PR + CI green, and `scripts/dev-setup.sh` updated with any new dep/tool/step.

**Status (post Wave 1 + H·B/H·C):** Tracks **R, H·A, H·B, H·C, S, N, D shipped** —
migrations **0042** (Track S reconciler seed), **0043** (Track N2 `skill_version`),
and **0044** (H·B nightly `eval_run` schedule). **H·B/H·C** landed at the owner's
direction (overriding the decision-#3 "park until a consumer" recommendation): the
nightly eval is a standalone production regression signal. Two deviations from the
original H·B sketch, both verified by red-team against the code: (1) **no `app.actions`
seed row** — `eval_run` is referenced by name through the in-code registry exactly as
`PURGE_ACTION`/the reconcilers are (a seed row would break the exact-six-row
`test_seeded_actions_are_globally_readable`); (2) the eval **runtime core + corpus were
relocated into the shipped package** (`jbrain/evals/runner.py` + `jbrain/evals/cases/`)
because `backend/evals/` is not in the prod image — without this the nightly schedule
would `PermanentJobError` every night in production. **Phase 5 is now closed:** the
`WORKFLOW_ENGINE_PLAN.md` archival is done (Track D, §6) and the one open owner
decision is resolved — **Loop-4 self-edit is deferred to Phase 6** (§5.2), carried
forward in `ROADMAP.md`. The per-track migration numbers below were the pre-build
estimates — see the assigned-at-merge rule in §2.

---

## 1. What already shipped (the substrate this finishes)

- **The engine** (`backend/src/jbrain/workflow/`): `dispatcher.py`,
  `scheduler.py`, `registry.py`, `runlog.py`, `events.py`, `contracts.py`. The
  dispatcher is LIVE (`worker.py:170-180`): it computes enqueue-equivalence,
  dedups note-keyed kinds (`_already_active`, `dispatcher.py:313-352`), and
  run-logs. The hardcoded triggers (note→ingest, ingest→integrate,
  resolution→consolidate) now emit events the dispatcher resolves
  (`analysis/repo.py:56-84,872-898`).
- **The scheduler** (`scheduler.py`): claims `app.schedules` by `next_run_at`
  SKIP-LOCKED, advances app-side, enqueues the bound pipeline; the nightly sweeps
  (migration 0038) and the 5-minute pending/integration reconcilers (0041) are
  data-defined schedules + `manual=true` Ops triggers.
- **The unified run-log**: `app.runs` / `app.run_steps` (`models/agent.py:47-114`,
  migration 0037) — the `agent_runs` rename, `session_id`/`prompt_version`
  nullable under a `kind='agent'` CHECK, `ran_as` scope stamp, `domain_code` /
  `principal_id` carrier (migration 0039).
- **The eval-harness gate** (shipped but unfed): the pure `promotion_decision`
  (safety-inclusive, fail-closed), `EvalRunStore` (owner-RLS), `PromotionService`
  (fail-closed on a missing run), `SelfImprovementGate` (separate per-day budget +
  kill-switch — `selfimprovement.py:43-77`), and the **opt-in** `eval_run` action
  (`workflow/evalaction.py`) deliberately kept out of the always-on registry to
  preserve the 0035 seed-lockstep, with **no live `Scorer`** and a **deferred
  `app.actions` seed projection**.
- **Reflexion (Loop 1)**: `agent/reflexion.py` — fully tested, pure, ephemeral,
  and **UNWIRED**. The old non-streaming wiring was removed (W0·e, commit
  c9788a5); it only ever lived on `AgentLoop.run` (`loop.py`), which `/chat` never
  calls. Production goes exclusively through `run_stream` (`loop.py:224`; caller
  `api/agent.py:242`).

> **Migration-number reality check.** Migrations run through **0044** (0042 = the
> Track S reconciler seed, 0043 = the Track N2 `skill_version` column, 0044 = the
> H·B nightly `eval_run` schedule). The ROADMAP/README/CLAUDE.md "Phases 0–4 shipped
> … through 0034 … Phase 5 not started" drift was corrected by Track D in this same
> wave.

### Carried-forward Phase-5 items: status (all accounted for)

The ROADMAP's "Carried forward from Phases 3–4" list is **already closed** by the
engine waves — recorded here so Phase-5-completeness is verifiable from this doc,
not only by re-deriving it from the code:

- **`extraction_truncated` review card** — ✅ shipped (W0·c; `dropped_facts` threaded
  through `plan_to_extraction`, `analysis/arbiter.py`).
- **`integration_run` + `resolution_pin` tables (N9/N10)** — ✅ shipped (W1·A4;
  `IntegrationRunLog` writes `kind='integration'` runs in `analysis/persist.py` /
  `analysis/pipeline.py`; `resolution_pin` in `models/workflow.py`).
- **N14 owner-ahead ordering** — ✅ seamed, teeth deferred to Phase 7
  (`INTEGRATION_BACKFILL_ORDER_BY` inert until `untrusted_origin` provenance exists;
  `WORKFLOW_ENGINE_PLAN.md §4`).
- **Agent-loop maturation** — `JobEnqueuedEvent` ✅ shipped (W0·e, `agent/loop.py`);
  `.tool` digest pins ✅ shipped (W0·e, `agent/toolfile.py` + read-tool guard);
  **reflexion-in-the-live-turn** is the one remainder → **Track R** below.
- **Merge proposals** — exist as review-card creation during analysis, **not** a
  periodic sweep; there is nothing to *migrate*. A periodic merge-proposal *sweep*
  is not-yet-built feature work → Phase 6 (§4).

So the only carried-forward item still open for Phase 5 is reflexion (Track R);
everything else shipped or is correctly seamed/deferred.

---

## 2. Scope in / scope out at a glance

| Track | Title | In Phase 5? | Size | Migration |
|---|---|---|---|---|
| **R** | Reflexion in the live turn | **In** (≈4 slices) | Medium | none (Loop 1 writes nothing) |
| **H** | Eval-harness completion A–C | **In** | Medium | 0042 (seed projection in B) |
| **S** | `backfill_unembedded_notes` reconciler | **In** | Small | 0043 (0041-style) |
| **N** | Three nits (N1 dedup, N2 `skill_version`, N3 FK record) | **In** | Small | 0044 (N2 column) |
| **D** | Doc drift + archival | **In** | Small | none |
| **L4** | Loop-4 prompt/tool self-edit + adversarial suite | **OWNER DECISION** | Large | none (reuses `.prompt`/`.tool` infra) |
| **F–G** | Loop 2 — skill learning | **Defer → Phase 6** | Large | — |
| **(H6)** | Loop 3 durable-knowledge + predicate-canon self-improve | **Defer → Phase 6** | Large | — |
| **(W6)** | Not-yet-built sweeps (entity hygiene / summary-reembed / tag-consolidation) | **Defer → Phase 6** | — | — |

**Migration numbers are assigned at *merge* time, not reserved.** A migration's
number = the next free revision and its `down_revision` = the then-current head
when it merges. The 0042/0043/0044 column above is **illustrative only** — it
assumes an H→S→N2 merge order, but these tracks run **concurrently** in Wave 1, so
do **not** hard-code a `down_revision` from this table: take the next free number
at merge and chain `down_revision` to the current head. (The per-track numbers
below carry the same caveat.)

---

## 3. In-scope tracks

### Track R — Reflexion in the live turn

**Framing.** `reflexion.py` is a fully-tested pure module that production never
calls. The core tension: **a stream cannot retry mid-flight** — by the time a
claim is suspect, its tokens are already on the wire (`loop.py:256-258`). And the
only production turn is `run_stream` (`/chat`). Reflexion writes **nothing** (Loop
1 is ephemeral — `reflexion.py:9-10`), so there is **no RLS / firewall / notes-door
concern** in this track; the entire risk surface is UX and verifier calibration.

Two implementation strategies for verifying the live turn:

- **(a) buffer-then-verify-then-retry** — hold the answer, run the verifiers,
  optionally re-produce (up to `MAX_RETRIES=2`), then stream the kept answer. Real
  UX cost: the spinner-not-typing tradeoff the owner flagged (no token streams
  until verification clears).
- **(b) verify-and-annotate** — stream normally, then after `DoneEvent` emit an
  **ungrounded-claim verdict event** (no retry; the answer the user saw stands,
  annotated). Zero streaming-latency cost; no mid-flight retry.

**Recommendation:** ship **(b) as the default** + **(a) opt-in, off by default**,
behind a settings gate, **heuristically triggered**. "Critique-worthy" =
surfaced sources OR a mutating tool was called OR a sensitive scope was touched —
greetings and chit-chat are never verified. Reflexion here is **NOT charged against
the self-improvement daily budget** (that budget — `selfimprovement.py` — is for
non-interactive pipelines; a live interactive turn must not be starved by a nightly
eval). `_GROUNDING_THRESHOLD=0.5` (`reflexion.py:28`) is **uncalibrated** and must
be tuned against the harness corpus before either mode ships on by default.

**Slices (≈4, sequential within the track):**

1. **R1 — trigger classification.** A pure `critique_worthy(turn) -> bool` from
   the turn's surfaced sources / tool calls / scope. Pure unit tests; no streaming
   touched. Unblocks R2/R3.
2. **R2 — streaming verify-and-annotate (default).** After the loop's terminal
   `DoneEvent`, run `aggregate(verify_citations, verify_grounding, ...)` over the
   streamed answer + the turn's retrieved sources, and emit a new
   ungrounded-claim verdict ChatEvent. The contract for that event is net-new (add
   to `agent/contracts.py`; the `/chat` SSE serializer in `api/agent.py` emits it).
   No retry. Off for non-critique-worthy turns.
3. **R3 — opt-in buffer-retry (mode a).** Behind a settings gate
   (`reflexion_buffer_retry`, default off). When on and the turn is
   critique-worthy, buffer the produce-step, run `reflect(...)` (`reflexion.py:179`,
   `MAX_RETRIES=2`, strict-improvement adoption), then stream the kept answer.
   The buffered path **reuses `run`'s non-streaming produce** so the two paths
   agree, then re-streams the deltas. Document the spinner-latency tradeoff in the
   settings description.
4. **R4 — calibration + tests + docs.** Tune `_GROUNDING_THRESHOLD` against the
   eval corpus (record the chosen value and the false-positive/false-negative
   counts in the PR); a regression test that an ungrounded claim surfaces a
   verdict event in mode (b) and triggers a retry in mode (a); update
   `docs/ASSISTANT.md` (Loop 1 is now wired in the live turn) and the settings
   reference.

**Sequencing/parallelism.** R is internally sequential (R1 → R2 → R3 → R4) but
**fully parallel to every other track** — it touches only `agent/` and adds no
table.

**Exit criteria.**
- A critique-worthy `/chat` turn emits an ungrounded-claim verdict event after
  `DoneEvent` (mode b) with no streaming-latency regression on non-critique turns.
- Mode (a) is reachable behind `reflexion_buffer_retry` (default off) and adopts a
  retry only on strict verifier improvement, capped at N=2.
- `_GROUNDING_THRESHOLD` is calibrated with the chosen value justified in-PR.
- Reflexion spend is **not** charged to the self-improvement budget.

**Test requirements.** Pure-unit for R1/R4 verifier logic; streaming integration
tests (fake LLM router, fake registry) for R2/R3 asserting event order and the
mode gate. No new table → **no RLS isolation test** (Loop 1 writes nothing — state
this explicitly in the PR so a reviewer doesn't expect one). 80% coverage; the
verifier + trigger paths are correctness-critical → push them to 100%.

---

### Track H — Eval-harness completion (A–C)

> **SHIPPED (H·A, H·B, H·C) — see the status block above.** Two corrections to the
> pre-build H·B sketch below, both proven against the code by red-team: there is **no
> `app.actions` seed projection** (a seed row would break the exact-six-row
> `test_seeded_actions_are_globally_readable`; `eval_run` resolves by name through the
> in-code registry exactly as `PURGE_ACTION`/the reconcilers do), and the eval runtime
> + corpus were **relocated into the shipped package** (`jbrain/evals/`) because
> `backend/evals/` is not in the prod image. The schedule fires 03:00 UTC (migration
> 0044), not "owner-local 02:00". Read the H·B/H·C bullets below as the original intent;
> the status block records what actually shipped.

**Framing.** The gate shipped; nothing feeds it. The highest-leverage quick win is
**A** — without a live `Scorer` the gate can never run on real model output.

- **H·A — live `Scorer` + DB-mode eval wiring (the quick win).** Implement the
  `Scorer` callable (`evalaction.py:44`) that drives the eval suite through the
  **LLM adapter** (never a provider SDK), returning `(EvalRun, tokens)`. Wire the
  `eval_run` action so it runs **behind the budget gate** (`SelfImprovementGate`
  already refuses fail-closed over budget / kill-switch — `evalaction.py:98-105`).
  CI injects a **fake scorer** (deterministic tokens, no model); the live one is
  opt-in. `eval_run` stays out of the always-on `ACTION_SPECS` to preserve the
  0035 seed-lockstep (`evalaction.py:5-11`).
- **H·B — nightly eval pipeline/schedule + the deferred seed projection.** Add an
  `eval_run` **pipeline** + a nightly **schedule** (mirror 0038's `_SWEEPS` shape;
  owner-local 02:00, `interval=1d`). In the **same migration (0042)** land the
  **deferred `app.actions` seed projection** for `eval_run`
  (`evalaction.py:22-25`) so the boot-validation name match and the seed-lockstep
  test both pass with the action now reachable as a pipeline step. The nightly run
  stores an `EvalRun`; `PromotionService` reads candidate↔baseline from
  `EvalRunStore` (already shipped).
- **H·C — fixture / new-case curation convention.** A written, binding convention
  (in `backend/evals/README.md`) for: where a new-case fixture lives, how a
  `new_case` label is chosen, who curates "the originating task class"
  (`docs/ASSISTANT.md:609`), and the two-dimensional `{task, safety}` score
  contract the gate depends on (a flat blob would defeat the gate). No code; a
  convention + one worked example fixture.

**Sequencing/parallelism.** H·A first (it unblocks B). H·A → H·B sequential
(B's schedule consumes A's action); H·C is independent and can land anytime.
The whole track is parallel to R, S, N, D.

**Open question this track must resolve (see §5):** is harness completion worth
doing *now*, given **no skill/prompt self-edit consumes it yet**? The harness's
sole purpose is to gate Loop-2 (skill) and Loop-4 (prompt) promotions, both of
which are Phase-6 (or the L4 decision). See §5 decision 3.

**Exit criteria.**
- The nightly eval schedule fires, runs a faked eval in CI / a live eval opt-in,
  and stores an `EvalRun` with the `{fixture, task, safety}` split intact.
- `eval_run` has its `app.actions` seed row (0042); the seed-lockstep test passes
  with the projection present.
- A budget-exhausted / kill-switched eval refuses fail-closed (no token spent) and
  does **not** retry (`PermanentJobError`).
- The curation convention is documented with one worked fixture.

**Test requirements.** Pure/fixture-driven gate and store tests (already the
pattern); the action faked in CI; **RLS isolation test for the `eval_runs` table**
must already exist from the engine wave — if H·B touches its columns, re-assert it.
The budget refusal path is security-adjacent → **100% coverage**. Real Postgres via
testcontainers for the store/schedule.

---

### Track S — `backfill_unembedded_notes` reconciler

**Framing.** Exactly **one** un-migrated sweep remains boot-only:
`backfill_unembedded_notes` (`worker.py:184`, `queue.py:537`). It still self-heals
only at boot — a dropped `embed_note` enqueue strands a note's chunks unembedded
until the next restart. This is a **near-mechanical mirror** of the two shipped
reconcilers (0041): add a `reconcile_unembedded_notes` action to the in-code
scheduler registry (`scheduler.py`, alongside `RECONCILE_PENDING_NOTES_ACTION` /
`RECONCILE_PENDING_INTEGRATION_ACTION` at `scheduler.py:79-92`) and a **0041-style
schedule + manual trigger** seed migration.

**Concrete shape (copy 0041 with three substitutions):**
- Action `reconcile_unembedded_notes`, handler same name, wrapping the existing
  `queue.backfill_unembedded_notes` INSERT…SELECT (no new query).
- A `pipelines` + `schedules` + `triggers` seed row (one entry in a `_RECONCILERS`
  tuple), `interval_seconds=300`, `next_run_at=now()`, `manual=true`.
- A fresh stable schedule/trigger UUID pair (continue the `…000c00xx` series).
- **Keep the boot backfill** (belt-and-suspenders, exactly as 0041 did —
  `worker.py:184` stays).

**Migration:** shipped as **0042** (`0042_seed_unembedded_reconciler_sweep.py`),
`down_revision = '0041'` — H·A needed no migration, so S took the first free number.

**Sequencing/parallelism.** Fully independent, small, parallel to everything.

**Exit criteria.** A note with NULL-embedding chunks and no live `embed_note` job
self-heals within ~5 minutes via the schedule (not just at boot); the reconciler is
emergency-fireable from Ops; the boot backfill still runs.

**Test requirements.** Mirror the 0041 reconciler tests: a testcontainers test that
the action enqueues `embed_note` for an unembedded note and skips one with an
active job (idempotent); the scheduler-tick test picks it up. **No new table** →
no RLS isolation test (seed rows only, into existing `pipelines`/`schedules`/
`triggers`, whose RLS tests shipped in 0036).

> **Out of scope (and correctly so):** the other roadmap-named sweeps — entity
> hygiene, summary re-embedding, tag consolidation, wiki build — **do not exist
> yet**. They are "build the sweep first," not "migrate an existing sweep," so they
> are Phase-6+ feature work, NOT migration work. Do not scope them here. (See §4.)

---

### Track N — Three nits

All Small; independent of each other and of every other track.

- **N1 — dedup `consolidate_predicates`.** Since the W2·C cutover,
  `consolidate_predicates` is enqueued by the event dispatcher off a
  `resolution.changed` event (`analysis/repo.py:56-84`), emitted on **every**
  remapping resolution. But the dispatcher's `_already_active` only dedups
  `{ingest_note, integrate_note}` (`dispatcher.py:288`) — a no-payload-key kind
  like `consolidate_predicates` is enqueued **unconditionally** every time. Add a
  **no-payload-key dedup branch** to `_already_active`: when the would-be enqueue
  has no note-key *and* a queued/running job of the same kind already exists,
  suppress it. **Mechanism (don't use `has_active`):** `has_active`
  (`queue.py:185`) is structurally payload-keyed (`payload->>:field = :value`) and
  **cannot** express a kind-only check. Add a small **kind-only active-check
  helper** modeled on `enqueue_sync_predicates_if_absent`'s guard
  (`queue.py:525-535`: raw `WHERE kind = :kind AND status IN ('queued','running')`)
  and call it from `_already_active` for payload-keyless kinds. **Migration:** none.
- **N2 — nullable `skill_version` on `app.runs`.** Add a nullable `skill_version`
  column to `app.runs` (migration **0044**) and the matching ORM field after
  `prompt_version` (`models/agent.py:88`). This is the deferred Track-C
  auditability item (`docs/ASSISTANT.md:615`) — `skills` is groundwork with no
  consumer, but stamping the column now means a Phase-6 skill-promoted run is
  auditable without a later schema change. Column only; no logic writes it yet.
  Reversible migration; the `runs` RLS test re-asserted.
- **N3 — record the `RunStep.job_id` asymmetry (no code change beyond a test).**
  `Run.session_id` is **fine** — it is a proper ORM FK to `agent_sessions`
  (`models/agent.py:70-72`). The real asymmetry is `RunStep.job_id`
  (`models/agent.py:114`): a **plain uuid with no ORM FK**, because `app.jobs` is
  the `queue.py` raw-SQL substrate and is **not a mapped table** (an ORM FK would
  fail mapper resolution). The **DB-level FK exists** (ON DELETE SET NULL,
  migration 0037). This is **deliberate and already documented in the docstring**
  (`models/agent.py:110-114`) — N3 is just to *record* it here, optionally adding a
  testcontainers test that asserts the DB FK + its SET NULL behavior (a job aged
  out of `app.jobs` nulls `run_steps.job_id` rather than breaking a run-log read).
  **Numbering note:** the as-shipped code comments label this `job_id`-FK item
  "(N2)" (`models/agent.py:97,113`) from an earlier task-numbering epoch; this plan
  calls it **N3** and uses **N2** for `skill_version`. Same item — don't
  cross-reference the wrong tag. **Migration:** none.

**Sequencing/parallelism.** N1, N2, N3 are mutually independent and parallel to all
other tracks. N2 carries the only migration in the track (0044).

**Exit criteria.** N1: a second `consolidate_predicates` is suppressed while one is
queued/running (regression test). N2: `app.runs.skill_version` exists, nullable, on
table + ORM. N3: the FK asymmetry is documented here; the optional DB-FK test
passes if added.

**Test requirements.** N1 dispatcher dedup unit + testcontainers (security-adjacent
— it is dedup of a mutating sweep → keep the dedup path at 100%). N2 reversible
migration + re-assert `runs` RLS. N3 optional DB-FK behavior test. 80% overall.

---

### Track D — Doc drift + archival

**Framing.** The cutover shipped but the docs still say Phase 5 is "not started"
and migrations run "through 0034." Fix the drift and archive the completed plan.

**Concrete edits (each is load-bearing — verify line context, it may have moved):**
- `docs/ROADMAP.md`: the **Status** block (`:8,12,16-17`) — "Phases 0–4 are
  shipped … migrations run through 0034" and "Next: Phase 5 … (not started)" — and
  the **Phase 5 header** (`:103`, "◀ Next"). Update to: Phase 5 engine + scheduler +
  run-log + cutover shipped; migrations run through 0041; residual completion
  (this plan) in flight; the deferred loops named to Phase 6.
- `docs/README.md`: the **Where the project is** block (`:11,13,15`) — migrations
  "through 0034", "Next: Phase 5" — and the **Active plan** entry (`:35-38`):
  archive `WORKFLOW_ENGINE_PLAN.md`, point "active plan" at this doc, and move the
  workflow plan into the Archive list **once the residual slices land** (not
  before — it is still the engine's build record while R/H/S/N are open).
- `CLAUDE.md`: the "Phases 0–4 are shipped" sentence → "Phases 0–4 shipped; the
  Phase-5 workflow engine + scheduler + run-log + cutover shipped (migrations
  through 0041); residual Phase-5 completion in `docs/PHASE5_COMPLETION_PLAN.md`."
- `docs/archive/WORKFLOW_ENGINE_PLAN.md`: **archive** it to `docs/archive/` once R/H/S/N
  land (it is the completed engine build record); leave a one-line pointer.

**Sequencing.** The drift edits (ROADMAP/README/CLAUDE.md migration-number +
status) land **immediately and independently** — they are wrong *today*. The
**archival** of `WORKFLOW_ENGINE_PLAN.md` is the last action of the phase (after
R/H/S/N merge), so it slots into Wave 2.

**Exit criteria.** No doc claims Phase 5 is "not started" or migrations end at
0034; `WORKFLOW_ENGINE_PLAN.md` is archived with a pointer once residual slices
land; this plan is the named active Phase-5 doc.

**Test requirements.** Docs only — run `markdownlint` if available; no code tests.

---

## 4. Explicitly deferred to Phase 6 (with rationale)

These are **out of scope for Phase 5 completion** and recorded here so a builder
does not pull them in. Each needs a spine this phase deliberately did not build.

- **Loop 2 — skill learning (Tracks F–G).** **LARGE.** The `skills` table is
  groundwork with **no consumer**. Closing the loop needs: skill **distillation**
  (turning successful episodes into reusable skills), **embedding + RRF retrieval**
  (a skill-fetch path), a **shadow → active promotion driver** gated by the eval
  harness, and **quarantine / eviction** for regressed skills. ASSISTANT.md stages
  this to Phase 6 (`:595`). Defer.
- **Loop 3 — durable-knowledge promotion + predicate-canon self-improvement.**
  Tier-B durable-knowledge promotion (`ASSISTANT.md:597`) and the
  predicate-canonicalization self-improvement loop (PREDICATE_CANONICALIZATION
  step 5: *agent proposes registry merges via correction notes* — `:208-210,277`)
  both require the **wiki / correction-note spine**, which is Phase 6. The
  correction-note machinery is the only sanctioned write lever back into the
  registry; without it there is nothing to promote through. Defer.
- **Not-yet-built sweeps.** Entity hygiene, summary re-embedding, tag
  consolidation, wiki build. These are roadmap-named but **the sweeps do not exist
  yet** — they are feature work ("build the sweep"), not migration work ("schedule
  an existing sweep"). Only `backfill_unembedded_notes` (Track S) is a real
  un-migrated existing sweep. Summary re-embedding additionally lives inline in the
  resolution read path today and was already flagged a *spike, not a committed
  migration* (`WORKFLOW_ENGINE_PLAN.md §7`). Defer all four.

---

## 5. Open decisions (for the owner)

These must be resolved before or during the relevant track's PRs.

1. **Reflexion default — mode (a) vs (b).** Recommendation: **(b)
   verify-and-annotate as the default** (no streaming-latency cost), with **(a)
   buffer-then-retry opt-in, off by default** behind `reflexion_buffer_retry`.
   The owner flagged the spinner-not-typing tradeoff of (a); (b) preserves the
   streaming UX and still surfaces ungrounded claims. **Owner: confirm (b)-default,
   or pick (a)-default if catching a bad claim *before* the user reads it outweighs
   the latency.**

2. **Loop 4 — in Phase 5 or defer to Phase 6? (the L4 decision track.)**
   **RESOLVED (2026-06): DEFERRED to Phase 6** — the owner took the recommendation
   below. Loop-4 self-edit and its 100%-coverage adversarial-injection suite are
   carried forward in `ROADMAP.md` Phase 6; Phase 5 closes without it. Loop 4 is
   prompt/tool **self-edit** as PR-shaped, human-gated proposals
   (`ASSISTANT.md:596`). It is **Phase-5-feasible**: it reuses the
   **fully-shipped** `.prompt`/`.tool` versioning + CI-pin infrastructure, needs
   **no new storage**, and is human-gated (every self-edit is a PR, not an
   autonomous write). **But** ASSISTANT.md stages it to Phase 6 (`:596`), and it
   carries a hard prerequisite: **non-negotiable #12** (`ASSISTANT.md:96-98`)
   requires an **adversarial-injection regression suite at 100%**, with the
   **data/instruction-boundary prompt and the domain-classification logic
   structurally barred from self-edit**. That suite is the **single most
   security-sensitive deliverable in this entire plan**. **Recommendation: DEFER to
   Phase 6**, aligning with ASSISTANT.md — the harness (Track H) that gates Loop-4
   promotions is only just being completed here, and shipping the self-edit lever
   *and* its adversarial guardrail in the same residual-completion phase over-loads
   it. If the owner wants it in Phase 5, it becomes its own track with the
   adversarial suite as a 100%-coverage red-team gate (§6), and Track H·C's curation
   convention must define the prompt-edit gating task class first. **Owner: in or
   out?**

3. **Is harness completion (Track H) worth doing now, with no consumer?** The
   harness's only purpose is to gate Loop-2 (skill) and Loop-4 (prompt) promotions —
   **both deferred** (Loop 2 → Phase 6; Loop 4 → owner decision #2). So H may be
   building a gate nothing walks through yet. **Two readings:** (a) H·A (the live
   `Scorer`) is the cheap, high-leverage win that makes the gate *real* and lets the
   nightly eval run as an early-warning signal on prompt regressions **even without a
   self-edit consumer** — worth doing standalone. (b) H·B/H·C (the nightly schedule
   + curation convention) arguably wait for their consumer (the L4 decision). 
   **Recommendation: do H·A now** (it stands alone as a regression-detection
   signal and unblocks #2 cheaply); **gate H·B/H·C on the L4 decision** — if Loop 4
   defers to Phase 6, ship H·A and park B/C with it. **Owner: confirm H·A-now /
   B·C-with-consumer, or do all of H now.**

4. **Reconciler interval for Track S.** 0041 used 300s (5 min) for the
   pending/integration reconcilers. Embedding is heavier than the bounded
   INSERT…SELECT reconcilers — confirm 300s is acceptable or lengthen
   (recommendation: keep 300s; the action only *enqueues*, it does not embed
   inline, and it is idempotent).

---

## 6. Waves & review gates

The tracks group into **two waves** that parallelize cleanly. The nits, the sweep,
and the doc-drift edits are independent and quick; reflexion and harness are the
substantive tracks.

### Wave 1 — parallel, independent slices (land first)
- **Track S** — the unembedded reconciler (0043). *(Independent, small.)*
- **Track N** — N1 dedup, N2 `skill_version` (0044), N3 FK record. *(Independent,
  small; N1/N2/N3 parallel to each other.)*
- **Track D (drift half)** — the ROADMAP/README/CLAUDE.md status +
  migration-number corrections. *(Independent; wrong today, fix immediately. The
  `WORKFLOW_ENGINE_PLAN.md` archival waits for Wave 2.)*
- **Track R** — reflexion R1→R4. *(Independent of S/N/D and H; internally
  sequential. The one substantive design track that touches only `agent/`.)*
- **Track H** — H·A (always), H·B/H·C **gated on decision #2/#3**. *(Independent of
  S/N/D/R; parallel to R.)*

### Wave 2 — close-out (after Wave 1 merges)
- Archive `docs/archive/WORKFLOW_ENGINE_PLAN.md` → `docs/archive/` with a pointer (Track D
  archival half).
- **(If the owner takes L4 in-Phase-5 per decision #2)** the Loop-4 prompt/tool
  self-edit track + its **adversarial-injection suite** lands here as its own
  gated wave — **the single most security-sensitive item in this plan** — with the
  data/instruction-boundary and domain-classification prompts structurally barred
  from self-edit (non-negotiable #12) and the suite at **100% coverage**.
  Otherwise Wave 2 is just the archival.

**Review gates (no wave skips its gate):**
1. **Agent review pass:** `/code-review` for correctness + reuse on every wave's
   diff. For the security-touching items — N1 (dedup of a mutating sweep), H's
   budget/kill-switch path, and (if included) the L4 adversarial suite — also a
   **red-team pass + the `security-review` skill**, checked against CLAUDE.md
   non-negotiables and ASSISTANT.md I-#10 (budget) / I-#12 (self-edit boundary).
2. **CI gate:** lint, typecheck, tests green; **80% / security-100%** coverage;
   the seed-lockstep + `.prompt`/`.tool` digest pins run **as part of the test
   suite**; `dev-setup.sh` current.
3. **Human gate:** PR(s) reviewed + merged; the §5 open decisions resolved or
   explicitly carried.

---

## 7. Phase-5-complete exit criterion — ✅ all met (2026-06)

Phase 5 is **done** — every criterion below is satisfied:
- ✅ Reflexion (Loop 1) runs in the **live `/chat` turn** — annotating ungrounded
  claims by default (mode b), with the buffer-retry mode (a) reachable opt-in — and
  `_GROUNDING_THRESHOLD` is calibrated.
- ✅ The eval harness is **fed**: a nightly schedule (migration 0044) runs the eval,
  stores an `EvalRun` with the `{task, safety}` split, and `PromotionService` can gate
  a promotion off stored runs behind the self-improvement budget + kill-switch
  (H·A + H·B + H·C all shipped).
- ✅ **Every** existing periodic sweep is data-defined and on-demand triggerable —
  the last boot-only one (`backfill_unembedded_notes`) now self-heals on a schedule
  (Track S).
- ✅ The three nits are closed: `consolidate_predicates` no longer double-enqueues
  (N1); `app.runs.skill_version` exists for Phase-6 auditability (N2); the
  `RunStep.job_id` FK asymmetry is documented (N3).
- ✅ The docs tell the truth: no "Phase 5 not started," no "migrations through 0034";
  `WORKFLOW_ENGINE_PLAN.md` is archived; this doc is the completed Phase-5 record
  (now archived itself).
- ✅ The **owner decisions** (§5) are resolved — including the explicit in-or-out call
  on **Loop-4 prompt/tool self-edit**: **deferred to Phase 6** (carried to ROADMAP).

**Deferred to Phase 6 (not Phase 5 work):** skill learning (Loop 2), durable-
knowledge + predicate-canon self-improvement (Loop 3), and the not-yet-built
sweeps. This phase finishes the **runway** (engine, scheduler, run-log, a *fed*
eval harness, budgets, reflexion-in-the-turn, `skills`/`skill_version` schema) —
not the loops that run on it.
