# JBrain2 — Note→Graph Integrator (v3) Implementation Plan

The buildable plan for redesigning how notes become graph truth: replacing the
blind, per-note deterministic integration step with a **graph-aware Integrator
agent that owns judgment**, while the **deterministic arbiter keeps owning
structure, the firewall, and commit**. Grounded in the current codebase
(Phases 0–4 implemented: notes, ingest, search, the deterministic analysis
pipeline, RLS, the Postgres job queue, the agent loop + `.tool` registry +
Proposals, `.prompt`/`.tool` version guards, the schema registry, an eval
harness).

> **Status (2026-06): the core is SHIPPED and is the only path.** `integrate_note`
> (extract → Integrator → `plan_intent` → `apply_intent`) runs unconditionally;
> the v1 `analyze_note` step and the W3.3 cutover toggle/shadow-mode gate have been
> **removed** (see `docs/CUTOVER_V1_REMOVAL.md`). So the "deferred", "first cut",
> "before the trigger flips", and "additive alongside analyze_note" framing below
> describes pre-cutover history — Track A (arbiter) and Track B (Integrator),
> §9's Option 1 `apply_intent`, and the harness-scenario gate are live.
>
> **Still deferred / not yet built** (don't read as shipped): the `integration_run`
> and `resolution_pin` tables (runs log to structlog only; re-run convergence is
> currently carried by the arbiter's deterministic signals); **N14** owner-ahead
> ordering (`backfill_pending_integration` is oldest-first by `created_at` — the
> `provenance` column exists but isn't wired into the sort yet); §9's **Option 2**
> native id-based `apply_intent`; the read-tool traversal loop; and the Phase-5
> self-improvement loop. **Known gap:** the `extraction_truncated` review is no
> longer filed under integrate — `plan_to_extraction` rebuilds the `Extraction`
> with `dropped_facts=0`, so the cap still fires but no card is surfaced.

This plan is the product of two research rounds (5 option dossiers + a code
map; then 5 design-pillar dossiers + 2 adversarial red teams). Both red teams
**rejected** the "agent owns the writes" full-replace and **concurred** on the
v3 split below. Their condition lists are folded in here as binding invariants.

Every PR carries the project non-negotiables (adapter-only LLM, storage
abstraction, RLS-scoped sessions + an isolation test per new table,
tests-with-code at 80% / security-100%, Conventional Commits + PR + CI green,
`scripts/dev-setup.sh` updated with any new dep/tool/step).

---

## 1. The architecture in one paragraph

A note is captured, chunked, embedded, OCR'd, and FTS-indexed exactly as today,
then flagged **`pending_integration`** — ingest no longer auto-runs analysis.
The note→graph step is now three stages with a hard authority split:

```
1. EXTRACT  (kept, unchanged trust model): the single constrained `note.extract`
   call — JSON-schema-bounded, NO instruction channel — produces a stored raw
   Extraction (title, tags, mentions, candidate facts, temporal tokens).

2. INTEGRATE (NEW — the agent owns JUDGMENT): a bounded, distinct-principal
   Integrator agent reads the stored Extraction + traverses the live graph with
   READ-ONLY tools, and emits a validated `IntegrationIntent` — graph-aware
   coreference proposals, gender/attribute inference (weighted), relationship
   fan-out, and supersession *proposals*. It never writes the graph; it stages
   intent. This is where cross-note reasoning, gender, and relationship
   smartness live — the exact decisions the old per-note resolver was bad at.

3. ARBITRATE (the deterministic core — owns STRUCTURE, FIREWALL, COMMIT): the
   existing `_apply` machinery, hardened, consumes the IntegrationIntent and
   executes ALL structural mutations atomically in one transaction — key
   normalization, span anchoring, chain wiring, supersession candidate-scoping
   with the explicit domain filter, reciprocity, the retraction sweep, the
   citation/cross-subject firewall, the domain floor/ratchet. Runs under
   `SYSTEM_CTX` + explicit domain filter (NOT per-domain RLS — the general→health
   ratchet requires cross-domain write).
```

**The load-bearing rule:** the agent decides *what is true and who is who* (as a
proposal); the deterministic engine decides *how it is wired and whether it is
allowed* (and commits). The agent's non-determinism is bounded at one seam
(`IntegrationIntent`) and can never silently fork a chain or cross a firewall —
both red teams proved that is the only split that keeps the revision history and
the domain/subject firewall intact.

---

## 2. Non-negotiables for the Integrator (binding — from both red teams)

These extend CLAUDE.md and `docs/ASSISTANT.md`'s invariants. Security-adjacent
ones are at 100% coverage.

**Authority split**
- **N1. The agent decides semantics only; the deterministic engine performs all
  structural mutations.** Chains are *never* wired by the model. The agent
  proposes a link, a value, a kind, a supersede-vs-review verdict; it never
  writes `superseded_by`, never sets `note_id`, never commits.
- **N2. The Integrator is `mutate→staged` for ALL effects, never `mutate→direct`.**
  Identical write privilege to the chat assistant. The arbiter is the only writer.
- **N3. Identity/coreference, domain classification, the citation-chunk firewall,
  the cross-subject inverse gate, and supersession candidate-scoping stay
  deterministic in code** (`entities.py`/`extraction.py`/`pipeline.py`) and are
  **immutable to self-edit** (ASSISTANT.md #12). The agent *proposes* an entity
  link; the arbiter *validates* it (exists? same kind? not `distinct_from`? in
  scope?) and applies the safety policy. Any **cross-subject** attribution is
  force-staged with a flag — never silently committed.

**Integrity (data-integrity red team conditions)**
- **N4. `note_id` is genuinely immutable** once a fact is committed. The
  reciprocal-shadow-adoption case (today's `pipeline.py:1174` rewrite) becomes a
  **separate, audited `adopt_shadow` operation** gated on the new note actually
  attesting the edge at a verified span — never a side effect of an upsert.
- **N5. Destructive operations (the retraction sweep, chain repair) fire ONLY on
  an explicit `mark_integrated` for a fully-completed turn.** A budget/step-capped
  agent run is a **no-op** that leaves `pending_integration` set and writes
  nothing destructive. (One atomic arbiter transaction; never a partial write.)
- **N6. Single-owner attribution (I5):** every committed fact is owned by exactly
  ONE note (the one being integrated); `note_id` stamped unforgeably by the
  arbiter, so the existing retraction sweep + purge + `repair_chains` stay valid.
  Cross-note *synthesis* that can't attribute to one note is **not a fact** — it
  routes to review or an agent-authored note that owns its own facts.
- **N7. Supersession candidate reads run under owner scope with the explicit
  `domain_code` filter** preserved exactly as `_existing_facts` does today (so a
  health candidate can never retrieve/supersede a same-key general fact — W4),
  with an RLS isolation test asserting it.
- **N8. Closure is recorded as an explicit `closed_by_fact_id`**, retiring the
  `valid_to`-equality coincidence heuristic in `repair_chains` (`purge.py:181`).
- **N9. Total processing order is `(valid_from, reported_at, note_id)`** (matching
  `supersession._validity`), not raw `created_at`, so re-runs converge regardless
  of capture-time ties or backdated imports.

**Convergence**
- **N10. `resolution_pin`s** memoize the agent's identity + predicate-key
  decisions for re-run convergence, keyed on `(note_id, occurrence_index,
  span_text_hash)` — **never** raw offsets, **never** a zero-width/empty span
  (those route to review instead), invalidated on a `pinned` flip and re-anchored
  after edits. A **human `pinned` fact always wins** over a replayed pin.

**Weight / review**
- **N11. Weight is a deterministic ceiling, not the model's self-report.** Signals
  the tool/arbiter check (span-verified attestation, predicate-in-registry,
  object-entity-exists, supersede-vs-new) set the ceiling; the model's
  self-confidence may only *lower* within the band. Commit-vs-review reads the
  ceiling. **Floor-wins:** weight never promotes a fact past a per-kind
  supersession floor (measurement/event never auto-supersede; an inferred
  attribute conflict → review). Untrusted-origin facts get a hard weight cap
  (ASSISTANT.md #7 "normal, not elevated").

**Security infra (security red team conditions — required before real-LLM ship)**
- **N12. Principal-bound dual tool registries** + a CI test that the chat
  principal's registry contains **no** write/arbiter tool; two registries that
  cannot be co-loaded.
- **N13. Per-principal token + Proposal rate limiter** with a flood detector
  (auto-quarantine a burst); a real **usage ledger** + daily budget + kill-switch;
  the deterministic fallback must be **behaviorally identical** to the primary on
  every firewall decision (no engine-selection attack).
- **N14. Owner-authored notes are processed ahead of untrusted-origin notes** (so
  `OLDEST-FIRST` can't be gamed by a client-controlled capture time).
- **N15. Purge cascades** to derived chunks, derived inverse facts, Integrator-
  taught aliases, and `resolution_pin`s; **untrusted-origin notes never enact a
  merge** (ASSISTANT.md #11 "purge is total").
- **N16. No multi-turn tool loop carries untrusted note content as an instruction
  channel.** The Integrator loop is read-traversal + a final structured intent,
  `mutate` staged-only, **no memory/recall tools**, `max_steps` low (≤ 3).
  Extraction stays the single constrained call.

---

## 3. The seam: `IntegrationIntent`

The whole design hinges on one in-memory value object (mirroring how `_apply`
already consumes a pure `Extraction`). The agent produces it; the arbiter
validates and commits it. Bounding the agent's non-determinism here is what makes
the system testable and safe.

```
IntegrationIntent(note_id, schema_version, prompt_version, integrator_version):
  entity_resolutions: [                 # the agent's coreference judgment
    { mention_ref, proposed_entity_id | NEW(kind,name) | AMBIGUOUS,
      cross_subject: bool, attested_span: (chunk_id, surface), rationale }]
  facts: [                              # entity.predicate[.qualifier] edges
    { entity_ref, predicate, qualifier, kind, statement, value_json,
      object_entity_ref?, assertion, temporal_ref?, attested_span?,
      self_confidence, inferred: bool }]
  attribute_inferences: [ ... gender etc., always inferred+weighted ... ]
  supersession_proposals: [            # PROPOSAL only — arbiter wires the chain
    { target_key, action: supersede|conflict|accumulate|refresh, rationale }]
  merge_proposals: [...]  distinct_proposals: [...]   # always → review
  temporal_tokens: [...]
```

The arbiter treats every field as an *intent to validate*, never as an offset, a
domain, a chain pointer, or a commit. Invalid/ambiguous/cross-subject/
low-ceiling items degrade to the review inbox — the system's existing,
trusted failure mode.

---

## 4. Data-model deltas (each `domain_id` where applicable + RLS isolation test)

| Object | Change |
|---|---|
| `notes` | + `integration_state` (`pending_integration`/`integrating`/`integrated`/`stale`/`skipped`); retire the derived `analyzed` boolean in its favor. + `provenance` already exists (agent vs human) — reused for N14/N15. |
| `facts` | + `schema_version` (stamped beside `prompt_version`, N-schema); + `closed_by_fact_id` FK (N8). `note_id` made write-once at the arbiter (N4/N6). |
| `note_analysis` | + `schema_version`, + `integrator_version`. |
| `resolution_pin` *(new)* | `note_id`(FK,cascade), `occurrence_index`, `span_text_hash`, `decision_kind`(identity\|predicate_key), `entity_id`?/`normalized_predicate`?, `chunk_id`. PK `(note_id, occurrence_index, decision_kind)`. (N10) |
| `integration_run` *(new)* | one Integrator turn-loop: `note_id`, `status`, `step_count`, `cost_tokens`, `stop_reason`, `intent` jsonb, `integrator_version`, `schema_version`. Mirrors `agent_runs`; becomes a workflow `run` in P5. |
| `llm_usage` ledger | confirm/extend so `integrate.note` is metered; + the daily-budget query + kill-switch (N13). |
| `review_items` | + kind `low_confidence_inference` (N11); resolver dispatch branch. |
| schema registry | unify the YAML (`schema/defs/`) **and** the `supersession.py` frozensets into the one curated artifact; add the schema-version CI bump guard (Wave 2). |

---

## 5. Testing & LLM strategy — fake first, Grok later

**No real provider is called until the system is solid.** Two dev/test backends,
both implementing the existing `LlmClient` protocol (`llm/types.py`), swapped via
`JBRAIN_LLM_TASKS`/the router (`llm/router.py`):

1. **Scripted fake (CI, deterministic — mandatory per CLAUDE.md #5).** Extend
   `FakeLlmClient` (`llm/fake.py`) with fixture sets for `note.extract` (JSON)
   and `integrate.note` (scripted `LlmTurn` tool-call sequences). Every loop,
   guardrail, arbiter, convergence, and RLS test runs against this — fast, free,
   reproducible. The **convergence CI test** (N9/N10) uses a fake that returns
   *divergent but valid* coreference/kind choices run-to-run, asserting full chain
   isomorphism across both note orderings + an interposed no-op edit.

2. **"Claude-in-the-loop" dev backend (exploratory, the monkey-patch).** A
   `DevLlmClient` that, in an interactive dev session, surfaces the real assembled
   prompt and lets Claude author the model's response (extraction JSON / intent
   tool-calls) against real seed notes — i.e. Claude stands in for Grok. Its
   authored responses are **captured as fixtures**, which then (a) feed the
   deterministic scripted fake for CI and (b) form the seed corpus for tuning. So
   the dev backend is a *fixture factory*, not a runtime dependency, and never
   ships in a CI path.

3. **Real Grok (on token).** When the slice is green end-to-end on the fakes,
   wire the existing `openai_compat`/xAI client with the provided token, add the
   `integrate.note` task profile, run the calibration loop (rejection-rate
   telemetry per inferred-predicate, N11) on real notes, and tune thresholds. The
   scripted fake remains the CI gate forever.

**Cutover discipline (shadow mode):** before the trigger flips, run the new
extract→integrate→arbiter path in **shadow** against the current deterministic
`_apply` on the same notes, diff the resulting graph state, and gate on the
existing 100+ harness scenarios (`backend/tests/harness/scenarios/`). The clean
`IntegrationIntent` seam makes this diff cheap.

---

## 6. The waves (parallel tracks)

Dependency-ordered; each wave ends with a review + red-team gate (§7). Parallel
work runs in isolated worktrees, one short-lived feature branch per PR off the
integration branch.

### Wave 0 — Foundation & contracts (small; lands first; unblocks everyone)
- **W0.1 — The `IntegrationIntent` contract** (§3) as typed dataclasses + a
  validator stub; the shared shape every track builds against.
- **W0.2 — Note state machine:** `integration_state` column + migration + RLS
  test; **sever the ingest auto-enqueue** (`ingest/pipeline.py` ~124) so a note
  lands `pending_integration` after embed/OCR (preserve the OCR gate → flip-on
  semantics). `POST /api/notes/{id}/analyze` retained as a manual re-trigger.
- **W0.3 — Fake/dev LLM harness** (§5.1, §5.2): fixture format, `DevLlmClient`,
  extended `FakeLlmClient` for `integrate.note` turn scripts.
- **W0.4 — Migration DDL stubs** for every new table/column (§4), each with its
  RLS policy + an isolation-test stub.

### Wave 1 — four concurrent tracks

| Track | Owns | Builds independently | Integrates |
|---|---|---|---|
| **A — Arbiter hardening** (critical path) | Refactor `_apply` to **consume an `IntegrationIntent`** and **validate** (not decide) identity/supersession; N4 immutable `note_id` + `adopt_shadow`; N5 complete-turn-only destructive ops; N7 owner-scope+domain-filter existence reads; N8 `closed_by_fact_id`; N9 total order; cross-subject force-stage gate (N3). | the whole arbiter against scripted intents — no agent needed | the spine B feeds |
| **B — The Integrator agent** | the bounded integration loop (reuse `agent/loop.py`, distinct principal, `max_steps ≤ 3`, staged-only, no memory tools, N16); read-traversal tools (reuse `search`/`read_entity`/`find_entity`/`relate`/`read_fact`) + `read_extraction` + `list_pending_integration`; emit `IntegrationIntent`; system `.prompt` w/ data/instruction boundary (immutable-to-self-edit). | against the fake adapter | its intents drive A |
| **C — Convergence & weight** | `resolution_pin` table + hardened pin rules (N10); the **convergence CI harness** (N9/N10); the weight model (N11) — deterministic ceiling, per-predicate cap, untrusted-origin cap, `low_confidence_inference` kind, per-note inferred cap + dedup. | pure + fixture-driven | wraps A's commit + B's intent |
| **D — Security & safety infra** | N12 principal-bound dual registries + CI test; N13 rate limiter + usage ledger + budget/kill-switch + fallback≡primary; N14 owner-ahead ordering; N15 purge cascade + no-merge-from-untrusted; W4 RLS test (N7); per-table RLS tests. | migrations/services + tests | gates B/C before real-LLM |

**Critical path:** W0 → A (arbiter consumes intent) → B (agent emits intent) →
Wave-2 schema → Wave-3 integration. C and D overlap A/B entirely.

### Wave 2 — Schema evolution (agent-curated, versioned)
- **W2.1 — Unify** the YAML registry (`schema/defs/`) and the `supersession.py`
  frozensets (`FUNCTIONAL_/SYMMETRIC_PREDICATES`, `INVERSE_PAIRS`,
  `SCHEDULE_PREDICATES`) into the one curated artifact; `is_functional()`/
  `inverse_predicate()` read the registry. Stamp `schema_version` on facts +
  `note_analysis`. Add the **schema-version CI bump guard** (mirrors the
  `.prompt`/`.tool` digest-pin guard).
- **W2.2 — Novel-predicate path:** commit under the provisional-canonical key
  (open-vocabulary invariant — a novel predicate is a *stable isolated key, not a
  fork*) + file a `schema-change` Proposal; accept-as-is = zero migration;
  accept-renamed = `renamed_from` alias + in-place re-key via
  `consolidate_predicates`, **chain-merging same-canonical drift siblings** rather
  than stranding a collision (fixes A12).
- **W2.3 — Kind-reclassification migration:** atomic rewrite of `kind` on all live
  rows of a key + chain rebuild under the new semantics in one transaction; CI
  guard that no identity key holds mixed `kind` (fixes A10). Owner-gated + previewed.
- **W2.4 — All schema changes owner-approved** (never auto, untrusted-origin rule);
  classification logic immutable-to-self-edit.

### Wave 3 — Integration, bootstrap, cutover
- **W3.1 — Wire** extract→integrate→arbiter end-to-end; the `integrate_note`
  queue job + `integration_run` logging; **shadow mode** + the harness-scenario gate (§5).
- **W3.2 — One-time schema bootstrap:** deterministic SQL **mine** of corpus
  predicate usage / value shapes / reciprocal pairs (no LLM) → agent **drafts** v1
  YAML as a `schema-change` **Proposal tree** with mined evidence → owner approves
  whole/subtree/leaf → **dry-run re-key blast-radius report** → freeze as
  `schema_version: 1`; delete the `supersession.py` frozensets in the same PR.
- **W3.3 — Cut over** the trigger; batch processing oldest-first w/ owner-ahead
  ordering (N14); per-domain read scoping for the agent, `SYSTEM_CTX`+filter for
  the arbiter.
- **W3.4 — Real Grok** (on token): wire xAI client + `integrate.note` profile;
  calibration loop on real notes; keep the fake as the CI gate.

### Phase 5+ (carried forward)
`integration_run` becomes a workflow `runs` row; the trigger becomes an
`events→triggers→pipelines` def; nightly hygiene/consolidation becomes scheduled
triggers (replacing the boot backfills). The Integrator joins the self-improving
agent's eval/budget machinery.

---

## 7. Review gates between waves (no wave skips its gate)

1. **Agent review pass** over the wave's diff: `/code-review` for correctness +
   reuse; for any security/integrity-touching wave (the arbiter, the firewall, the
   classifier, pin logic, the registries, purge), also a **red-team pass** +
   `security-review`, checked explicitly against N1–N16 and the original A-/#-
   findings.
2. **CI gate:** lint, typecheck, tests green; 80% / security-100% coverage;
   `.prompt`/`.tool`/`schema` version guards; `dev-setup.sh` current; the
   **convergence isomorphism test** green.
3. **Human gate:** PR(s) reviewed + merged; open decisions resolved or carried.
4. **Iterate, then proceed:** the next wave fans out only once the gate is green.
   A **confirmation re-attack** of the two red teams runs against the integrated
   slice before Wave 3 cutover and before the real-Grok flip.

---

## 8. Open decisions carried forward

- The exact per-predicate weight ceilings and the commit threshold per kind
  (seed conservative; tune by rejection-rate telemetry).
- Daily integration budget + kill-switch dollar values.
- Whether the bounded Integrator loop is `max_steps` 2 or 3 (start at 2; raise
  only if traversal demonstrably needs it).
- Bootstrap: how much of the (to-be-wiped) corpus is regenerated vs seeded fresh.

---

## 9. Track A · A1b design (the DB executor) — grounded in the real pipeline

A1a (the pure `arbiter.plan_intent`) is done. A1b takes an `ArbiterPlan` + the
agent's `IntegrationIntent` and performs the writes through the existing
deterministic primitives. Reading the live code pinned the key facts:
`_resolve_entities` returns a **name-keyed** `dict[str, ResolvedEntity | None]`;
`_upsert_fact` consumes an **`ExtractedFact`**, runs `domain_floor`/`ratchet` →
`Candidate` → `_existing_facts` → `decide()`, with the fact's `confidence`
flowing through to the row.

**The impedance mismatch.** The pipeline is **name-based** (mentions carry
names; facts reference names; resolution maps names→entities). The intent is
**mention_ref-based with identity pre-resolved** (the agent supplies
`entity_id`s). A1b must bridge the two.

**Decision — A1b ships Option 1 (adapter + resolution override); Option 2 is a
later migration.**

- **Option 1 (chosen for A1b):** an additive `apply_intent(...)` that
  (a) synthesizes an `Extraction` from the plan (each committed/review
  `IntentFact` → an `ExtractedFact` with `confidence = plan weight`; each
  `EntityResolution` → an `ExtractedMention`), (b) builds a name-keyed
  `resolution_override: dict[str, ResolvedEntity | None]` from the agent's
  *validated* resolutions, and (c) calls the existing `_apply` with the override
  threaded into `_resolve_entities` (use the agent's entity when present —
  validated for existence / in-scope / not `distinct_from` — else fall back to
  the deterministic resolver; `ambiguous` → None + the existing card). This
  reuses `_rebuild_mentions`, `_upsert_tokens`, `_upsert_fact`, the sweep, and
  `decide()` **unchanged**. Smallest new surface; gets the end-to-end flow green.
- **Plan-review routing:** after `decide()` returns, a fact the plan marked
  `pending_review` is forced to `status=pending_review` and files a
  `low_confidence_inference` review item (new kind, Wave-1·C) — the arbiter's
  per-kind threshold is stricter than `decide()`'s `LOW_CONFIDENCE` guard, so the
  plan's decision must win. (Commit facts flow through `decide()` as today.)
- **Option 2 (deferred):** a native id-based `apply_intent` that never round-trips
  through names. Cleaner once the agent owns resolution end-to-end, but a large
  rewrite of mention/token/fact upsert — not worth blocking A1b.

**Decomposition (so as much as possible is locally verifiable despite no local
Docker):**
- **A1b-i (pure, local):** `plan_to_extraction(intent, plan) -> Extraction` — the
  IntentFact→ExtractedFact / EntityResolution→ExtractedMention adapter, weight→
  confidence. Unit-tested locally.
- **A1b-ii (DB, CI):** the `resolution_override` param on `_apply`/
  `_resolve_entities`, the validated `resolve_from_intent`, the plan-review
  forcing + `low_confidence_inference` card, and `apply_intent`. Integration-
  tested via testcontainers in CI; third-party-reviewed pre-push as the
  non-local safety net.

**Known bridge limitation (A1b-ii / A2+ inherits):** `IntentFact.attested_span`
(a fact's OWN chunk+surface) has no `ExtractedFact` home, so under Option 1 a
fact's citation falls back to its mention's span (or `chunks[0]`) via
`_rebuild_mentions`/`anchor_for`. Acceptable for the happy-path first cut;
restoring fact-level provenance (the agent already supplies it) is a follow-up —
likely via the resolution-override carrying spans, or the native Option-2 path.

**Invariant carry-forward:** A1b is the happy-path flow only. N4 (immutable
`note_id` + `adopt_shadow`), N5 (complete-turn-only sweep), N8
(`closed_by_fact_id`), N9 (ordering + domain-filtered existence reads) land in
A2–A6 on top of this; A1b must not contradict them (e.g. it keeps the existing
single-transaction `_apply`, so N5's no-partial-write holds by construction).
