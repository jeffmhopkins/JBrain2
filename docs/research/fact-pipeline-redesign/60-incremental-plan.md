# Incremental implementation plan (per decision D3)

Evolve the **existing** system; do not rebuild. Each wave is its own branch + PR, CI-green,
tests in the same PR (CLAUDE.md), every new table ships its RLS isolation test. The greenfield
final spec (`40-final-spec.md`) is the design reference for the adopted mechanisms; per-domain
projections and two-stage extraction are **shelved** (D3). GUI work is gated by **D2** (three
interactive mockups first).

Notes are the sources of truth, so any pipeline-affecting wave ends with a **re-ingest** of notes
under the improved pipeline (D1 re-scoped) — an online data refresh, not an architecture wipe.

---

## Wave 1 — Negation safety (modality in the live selection)
*The one shipped correctness gap; small and contained.*
- **Migration:** index support for an asserted-only live floor; no table rewrite (the `assertion`
  column already exists).
- **`analysis/supersession.py`:** the live-slot identity for selection includes `assertion`
  polarity; a `negated`/`hypothetical` value never supersedes an `asserted` of opposite polarity —
  an asserted+negated pair on the same `(subject, entity, predicate, qualifier, value)` routes to
  a contradiction review item instead.
- **`current()` everywhere** (entity view, wiki, review live-floor): filter to `assertion='asserted'`
  so non-asserted modalities never read as current.
- **Tests:** negation can't overwrite assertion; non-asserted excluded from current; RLS.

## Wave 2 — Structured-editing review (collapse the kind-zoo)
*Builds directly on the per-field editing shipped in #234/#236.*
- **D2 first:** three interactive HTML mockups of the unified editable-fact card under
  `docs/mocks/`; owner chooses.
- **Frontend:** evolve the review card into one structured editor exposing every field
  (predicate picker, typed value editor, subject/object relink, temporal, modality, domain) with
  **explicit add / replace / remove** for set predicates — the affordance driven by
  `is_functional(predicate)` (already authoritative). Kind becomes a sub-editor selector, not a
  card class.
- **Backend:** a structured edit-submission that maps edits onto the existing resolve / correction-
  note machinery now, and onto the op-layer once Wave 3 lands.
- **Tests:** per-field edits; functional→single vs set→add/replace/remove; firewall gates on
  relink/domain.

## Wave 3 — Arbitrary-order undo (the typed-op + audit layer)
*The biggest new subsystem; the one thing the current system can't do (Decision 3). Additive.*
- **New tables (RLS + isolation tests):** `fact_op` (typed op, actor, source, target slot,
  payload, frozen `resolved_outputs` + pipeline-version tuple, batch_id) and `fact_audit`.
  Record **every** mutation — extraction commits, supersessions, and human edits — as ops over
  the existing `facts` history (the supersession chain stays; ops annotate it).
- **Selective replay undo:** undo any op → recompute the affected `(subject, entity, predicate,
  qualifier[, value_identity])` slots from their short op subsequence, skipping undone ops;
  frozen resolutions for determinism; **firewall/domain re-derived live**; frozen links
  re-validated against the current firewall (verify V-S1). A genuine read-dependency surfaces one
  later op as a review conflict.
- **Migrate `reopen_review`** effect-reversal into the op model; expose "undo this" on any op plus
  "undo last" / "revert to point" (D2 mockups for the surface).
- **Tests:** non-LIFO undo correctness; un-tombstone re-derives domain; replay determinism; RLS.

## Wave 4 — Small adopts
- Stable `value_identity` for **scalar** set members (object-valued already keyed by
  `object_entity_id`): mint a member id where no natural key exists, carried by supersession —
  makes scalar typo-fix-vs-add clean.
- TypedValue tightening only if eval flags value-shape drift (the validator already covers most).

---

## Shelved (revisit on an explicit trigger)
- **Per-domain entity projections** — keep global tables + RLS. Revisit only if the system goes
  multi-user or gains untrusted agents that operate in one domain but could probe another.
- **Two-stage extraction** — keep single-stage + deterministic backstops. Revisit only if the eval
  harness shows the model invents predicates/links that canonicalization can't repair.

## Sequencing & risk
Wave 1 (high value, low risk) → Wave 2 (visible win, GUI-gated) → Wave 3 (big, additive, last) →
Wave 4 (polish). Waves 1 and 3 touch the pipeline → end each with a notes re-ingest. None requires
discarding the existing schema; all are online migrations + additive tables. Preserve the shipped
nuance listed in D3.
