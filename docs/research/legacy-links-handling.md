# Design research: handling past / "legacy" relationship links

**Status:** Design only — no source files modified. Active research (not yet
archived); moves to `docs/archive/research/` once a plan ships.
**Trigger:** Note *"I work for SpaceX, I used to work for the US army and Oregon
Lithoprint"* (captured 2026-06-15). The Analysis tab rendered the two **past**
employers (US army, Oregon Lithoprint) as plain `worksFor` edges with no
"former/ended" marker — and the **current** employer (SpaceX) never became a
`worksFor` edge at all (it survives only as the `spacex` tag).
**Owner idea to evaluate:** *"maybe each predicate could have optional temporal
tokens, one being 'in the past from now'."*

This dossier grounds that idea in the shipped pipeline, separates the two
distinct failures the note exposes, lays out the options, and recommends a
sequenced fix. Read alongside `docs/ANALYSIS.md` ("Facts", "Temporal model",
"Fact kinds and supersession") and `docs/entity.md` (the soft-schema registry).

---

## 1. The two failures (they are separate, fix them separately)

The screenshot looks like one bug but is two:

| # | Failure | Layer | This dossier |
|---|---|---|---|
| **A** | A past relationship ("used to / former / ex- / left") is stored as an **open, current-looking** `worksFor` edge — no `valid_to`, no "former" affordance. | extraction signal + supersession + display | **Primary focus** |
| **B** | The **current** employer ("I work for SpaceX") was demoted to a tag instead of emitted as its own `worksFor` edge. | extraction quality (prompt) | Noted; fixed in passing by the prompt work in §5 |

Failure A is the substance of the owner's question. Failure B matters because
the two interact: pure supersession (Option D) can *only* mark old jobs as past
if a current job edge arrives to supersede them — and here it never did.

---

## 2. What the shipped pipeline already does (grounded)

The good news that reframes the whole design: **the storage and most of the
machinery for "past from now" already exist.** What is missing is the *signal*
at extraction and the *legibility* at display — not the data model.

### 2.1 The bi-temporal model is ready, and `valid_to` is model-drivable

- Fact rows carry `valid_from` / `valid_to` (world time) vs `reported_at`
  (capture time), plus `temporal_precision ∈ {instant, day, month, year, era,
  unknown}` and an optional `temporal_token_id`
  (`migrations/versions/0006_analysis_schema.py:152-178`;
  `models/analysis.py:150-160`).
- The model's extracted `temporal.resolved_end` is mapped **straight onto
  `valid_to`** at insert — it is *not* only set by supersession:
  `pipeline.py:1843-1846` (`valid_to = fact.temporal.resolved_end`) →
  `pipeline.py:1980` (`valid_to=decision.insert_valid_to or valid_to`).
- So an **open-ended past interval** — `valid_to ≤ now`, `valid_from` unknown,
  `precision = era|unknown` — is representable **today** with zero schema
  change. (The "a fact must never gain a valid_to" comment in
  `extraction.py:363` is narrowly about *token part-of-day enrichment*, not
  about the model emitting an end date.)

### 2.2 Supersession already lands a closed interval as history

- `_interval_close` (`supersession.py:341-371`): a candidate that restates an
  open `state`/functional-`relationship` with the **same** object/`valid_from`
  but supplies a `valid_to` closes that row **in place** (SCD-2), no chain, no
  review. This is the "I actually left Acme back in March" path.
- The **retrospective branch** (`supersession.py:491-498`): a candidate about
  an *older* validity period inserts as `status="superseded"`,
  `superseded_by=current.id`, `valid_to = candidate.valid_to or current.valid_from`
  — it **never displaces the current value**.
- **Concurrent past values are already supported.** Each retrospective fact is
  an independent `superseded` row chained to the current head; newest-wins only
  guarantees a *single active* value, it does **not** collapse history. So "US
  army (past) + Oregon Lithoprint (past) + SpaceX (current)" is a legal graph
  state — three rows, two superseded, one active.

### 2.3 There is already a precedent for the *future* mirror of this

- `normalize_future_assertion` (`extraction.py:191-203`) is **live**, called
  per-fact at `pipeline.py:1613` and `pipeline.py:1815`: a fact whose validity
  is in the future and is marked `asserted` is relaxed to `expected`. It is the
  exact deterministic-guard pattern a *past* handler would mirror. **There is no
  past analog.**

### 2.4 "Ongoing vs ended" is already a registry concept — but in the wrong shape for this

- `schema/defs/facets.yaml` (`Temporal` facet) declares explicit `startDate` /
  `endDate` / `effectiveDate` predicates with the note *"endDate absent ⇒
  ongoing."* So the *concept* exists — but as **separate date facts**, not as a
  property of `worksFor` itself. Nobody models "I worked at Acme 2020–2023" as
  `Acme.endDate`; it is a `worksFor` edge with `valid_to` (§2.1).
- The `Predicate` dataclass (`schema/models.py:30-50`) has **no temporal facet
  field** and the registry exposes **no temporal query method**. So the owner's
  "per-predicate temporal token" would be a genuine new extension point — see
  §4, Option A, for whether it earns its keep.

### 2.5 Where it actually breaks

1. **No past signal at extraction.** The prompt (`note_extract.prompt`,
   `PROMPT_VERSION = note-extract-v17`) teaches kind-vs-tense and `expected` for
   the future, but **never** teaches "used to / former / ex- / no longer / left
   / previously" → a closed interval. A grep of `extraction.py` and the prompts
   finds zero recognition of these phrases. So "used to work for US army" lands
   as an **open** `worksFor` state ⇒ reads as current.
2. **A supersession gap when there is no current value.** The retrospective
   branch chains onto `current.id`. But if *every* value is past (no active
   head), a closed-interval candidate skips that branch and hits "no actives →
   insert active" (`supersession.py:446-447`) — making a **past** fact the
   **active** head. `_interval_close` also only matches an *open* existing row,
   so a first-and-only "I used to work at X" has nothing to close.
3. **The derived inverse confirms the misclassification.** Inverse edges are
   materialized **only** when `insert_status == "active"`
   (`pipeline.py:2016-2020`). The screenshot's "US army employs Me" edge
   therefore *proves* US army was stored as a current job — a correctly-past
   relationship would have **no** inverse.
4. **Consumers cannot show "past."** `graph_context` loads `status='active'`
   only (`graph_context.py:214,275`) — the integrator never sees history. The
   Analysis tab filters to `{active, pending_review}` (`AnalysisTab.tsx`), so a
   *correctly* superseded job would vanish from the note's own analysis with no
   "former" affordance; `factSpan` ("Mar 2019 → Jun 2026", `format.ts`) and the
   muted `superseded` chip (`bits.tsx`) live only in entity history rails.

---

## 3. The design question, precisely stated

How should "used to / former" be represented so that it (a) is **not** the
active head, (b) is **legible** as past, (c) coexists with other past values,
and (d) does **not** require inventing fake dates the note never gave?

The owner's "in the past from now" is exactly: **a closed interval whose end is
"at or before capture" and whose bounds are otherwise unknown** — i.e.
`valid_to = anchor`, `valid_from = null`, `precision = era|unknown`. The
question is *where that intent is expressed* and *what enforces it*.

---

## 4. Options

### Option A — The owner's idea: a per-predicate temporal "stance" (current | past | future)

Add a discrete stance the model emits when it knows a relationship is past but
cannot date it. Two sub-variants:

- **A1 — stance as a new field** (on the fact or its temporal block), with the
  pipeline translating `past → valid_to=anchor, precision=era`.
- **A2 — stance gated per predicate** in the registry (a `Predicate.temporal`
  facet declaring which predicates may carry a stance / are interval-bearing).

**Assessment.** The *instinct is right* — "past-from-now" is a real, common
shape the pipeline must capture. But a **new discrete field is mostly redundant
with `valid_to` + `precision`** (§2.1): "past, end unknown" already *is*
`valid_to=anchor, precision=era|unknown`. Adding a parallel stance column risks
two sources of truth for one fact (which wins if `stance=past` but
`valid_to=null`?). The cleaner realization of the owner's idea is a
**resolution convention over the existing fields**, not a new column (see the
recommendation). The **per-predicate gate (A2) earns its keep only if we need to
*restrict* stance** — e.g. forbid a stance on an `attribute` like `birthDate`
(timeless) while allowing it on `worksFor`/`residence`/`memberOf`. That is a
real guard, but it is an *enhancement* on top of the convention, not the
mechanism itself.

- **Pros:** Names the concept explicitly; a registry gate (A2) could stop "used
  to" being applied to nonsensical predicates; aligns with the owner's mental
  model.
- **Cons:** A1 duplicates `valid_to`+`precision`; new schema column + migration
  + RLS-untouched-but-still-tests; the registry has no temporal hook today so
  A2 is non-trivial new machinery; risks divergence between stance and the
  interval fields the rest of the pipeline already reads.
- **Effort:** A1 M, A2 L.

### Option B — Symmetric deterministic guard: `normalize_past_assertion` (closed-interval-from-phrase)

A direct analog to the live `normalize_future_assertion` (§2.3). A pure
function over `(ExtractedFact, anchor)`, called at the same `pipeline.py`
sites: when a `state`/functional-`relationship` fact has **no** `valid_to` and
its statement/phrase matches a **closed set** of past markers — *used to, used
to be, former(ly), ex-, no longer, previously, back when, left, was a … (until)*
— set `resolved_end = anchor` at `precision = era|unknown`, so it lands as
closed past history via the machinery in §2.2.

- **Pros:** Fully **CI-testable** (pure function, no model, no DB) — the
  decisive advantage the `fix-options/3` dossier hammered; **no PROMPT_VERSION
  bump**, no re-extraction; mirrors an established pattern maintainers know;
  observable via a `analysis.temporal_closed_past` log as a prompt-tuning
  signal.
- **Cons:** Phrase detection on the statement is crude — "former" can attach to
  the wrong clause; the closed set misses novel phrasing; best as
  defense-in-depth, not the sole fix.
- **Effort:** M (the unit-test matrix is the bulk).

### Option C — Prompt teaching (the model emits the closed interval itself)

Bump `note_extract` to teach the model to (i) set `resolved_end = capture
anchor` at `era`/`unknown` precision for a state/relationship stated as ended,
**and** (ii) always emit the stated current value as its own edge (this is the
fix for Failure B — SpaceX-as-tag). Bump `PROMPT_VERSION` → budgeted corpus
re-extraction (`docs/ANALYSIS.md` "Reprocessing", ≈$0.01/note).

- **Pros:** Fixes both failures at the source; the model has full sentence
  context a phrase table lacks (handles novel phrasing); fixes Failure B which
  no other option addresses.
- **Cons:** **Not CI-testable** (the harness cannot exercise the prompt — only a
  live eval can); probabilistic; costs a re-extraction migration.
- **Effort:** S (text) + the migration budget.

### Option D — Pure supersession (status quo)

Do nothing new; rely on a later/current value to supersede the old.

- **Verdict: rejected as insufficient.** It fails this exact note twice: the
  current SpaceX edge was dropped (Failure B), so nothing arrived to supersede;
  and even *with* SpaceX, two past employers stated in **one** note with no
  current value would mis-supersede each other under functional newest-wins (or
  hit the "no actives → active" gap, §2.5.2) rather than both landing as past.

---

## 5. Recommendation — hybrid, sequenced deterministic-first

Mirrors the `fix-options` doctrine: move every correctness claim that *can* be
deterministic into CI-tested code first, then layer the prompt gains as
defense-in-depth behind it.

### Phase 1 — Supersession correctness (CI-testable, no prompt bump) — ship first

1. **Close the no-active-head gap (§2.5.2).** A candidate carrying a `valid_to`
   (a closed interval) must insert as **closed history** — `status="superseded"`
   (or active-but-closed if it is genuinely the most recent past value) — even
   when **no active head exists**, instead of becoming the active head. This is
   a targeted change in `supersession.decide()` with unit tests for:
   first-and-only past job, two concurrent past jobs + no current, two past +
   one current.
2. **`normalize_past_assertion` deterministic guard (Option B).** Closed past
   marker set + closed-interval stamping; unit-tested over the phrase matrix and
   anchor cases. Wired at the same per-fact sites as `normalize_future_assertion`.

*Both are pure functions / pure decision logic — full CI, no migration, no
re-extraction.*

### Phase 2 — Represent "past-from-now" as a convention, not a new column (the owner's idea, refined)

3. Adopt the **resolution convention**: past-but-undated ⇒ `valid_to = anchor`,
   `valid_from = null`, `precision = era|unknown`. Reuses every field §2.1–§2.2
   already read; no schema change. This *is* the owner's "in the past from now"
   token — realized over existing storage rather than a parallel stance field
   (Option A1 rejected for redundancy).
4. **Optional registry gate (Option A2), only if needed:** add a
   `Predicate.temporal` facet to *restrict* the past convention to
   interval-bearing predicates (employment/residence/role/membership) and forbid
   it on timeless `attribute`s. Defer until Phase 1 logs show the guard
   over-firing on the wrong predicates — do not build speculative machinery.

### Phase 3 — Prompt v-bump (Option C), batched into one migration

5. Teach `note_extract`: (a) ended state/relationship → `resolved_end = anchor`
   at era/unknown precision; (b) **always** emit the stated current employer as
   its own `worksFor` edge (fixes Failure B). One `PROMPT_VERSION` bump, one
   budgeted re-extraction. The Phase-1 guard means a model lapse is still caught.

### Phase 4 — Display legibility

6. Give the Analysis tab a **"former / ended" affordance** so a correctly-past
   relationship is visible *in the note's own analysis* — surface `valid_to` /
   a "former" chip rather than only relegating it to entity history rails
   (today superseded facts are hidden from the tab entirely, §2.5.4).

### Phase 5 — Eval

7. Add a corpus case = **this exact note**. Assert: SpaceX active `worksFor`
   edge; US army + Oregon Lithoprint as **closed past** history (not active, not
   superseding each other); **no** derived inverse on the past edges; the tab
   shows them as former.

---

## 6. Open decisions for the owner

1. **End instant for "used to": `valid_to = anchor` vs a NULL-bounded "closed
   but unknown end".** Recommend `valid_to = anchor` at `era`/`unknown`
   precision — we *know* it ended at or before capture, and a concrete end keeps
   ordering/queries simple. `valid_from` stays null/unknown.
2. **Most-recent past with no current job: superseded, or active-but-closed?**
   If the owner has *no* current employer, should the latest past job be the
   "active" (but closed) head, or all-superseded with no head? Recommend
   **active-but-closed** for the single most-recent past value so "current
   employer = none, last = X (ended 2018)" reads correctly; older ones
   superseded. (Decides the exact shape of the Phase-1.1 fix.)
3. **A2 registry gate now or later?** Recommend **later** (defer until logs
   justify it).
4. **Should past relationships ever be visible to the integrator?**
   `graph_context` is active-only today. Role-reference resolution ("my *old*
   boss") would need history — out of scope here, but flag it so Phase 1 doesn't
   foreclose it.

---

## 7. CLAUDE.md alignment

- **#1 LLM-via-adapter / #2 storage / #3 RLS:** Phases 1–2 add no LLM calls, no
  file I/O, no new tables (convention over schema). If A2 (registry gate) or any
  new column is built later, it ships with its tests; no new RLS-scoped table is
  required by the recommended path.
- **#5 tests in the same PR:** Phase 1 is pure-function logic → unit-tested to
  the 80% gate in its own PR; Phase 3's prompt change needs the out-of-CI
  live-model eval (§5.7) as the pre-merge gate, acceptable because Phase 1 is the
  deterministic safety net.
- **#6 Conventional Commits / branch + PR:** one PR per phase, e.g.
  `fix(analysis): land closed-interval relationships as past history` (Phase 1),
  `feat(analysis): teach past-tense relationships + always-emit current employer`
  (Phase 3).
- **#7 wiki machine-written:** unaffected — corrections still flow through the
  review inbox / correction notes.
- **#8 dev-setup.sh:** no new dependency in the recommended path.

---

## 8. Status

Design only — no prompt, schema, pipeline, or test code modified. Next step is
the owner's call on §6 (especially decision 2, which fixes the exact screenshot)
and which phase to authorize first. The suggested first increment is **Phase 1**
— self-contained, fully CI-testable, no migration, and it directly stops a past
job from masquerading as current.
