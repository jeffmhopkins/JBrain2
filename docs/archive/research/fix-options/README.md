# Fix options — the three lapses found this session

Design dossiers (options + recommendation per issue; **no code changed**). Each
linked doc compares fixes across dimensions — prompt engineering, schema,
pipeline/architecture, deterministic validation, and verification. This README
is the decision surface: the recommended fix per issue, the dependencies
between them, and a suggested sequencing.

## The three issues

| # | Lapse | Layer | Recommended fix (see doc) | Effort | Re-extraction? |
|---|---|---|---|---|---|
| 1 | Non-subject persons dropped (object/possessor/pronoun never become entities) | model/prompt | Phase 1 **detection** (tag-vs-mentions check, orphan-ref warning, `xfail` scenarios) → Phase 2 **prompt v5** (non-subject rule + Person↔Person worked example) | S → M | Phase 2 only |
| 2 | No mutual/inverse edge (`Jeff.spouse→Celine` never yields `Celine.spouse→Jeff`; no `parent_of`/`child_of`) | pipeline/architecture | **Pipeline-derived materialized inverse** + code-constant inverse registry beside `FUNCTIONAL_PREDICATES`; cross-subject inverses routed to review, never written | M → L | No (derived) |
| 3 | Relative time resolves to wrong day ("last night" → capture day) | model/prompt | **Hybrid**: deterministic `validate_backward_temporal` (analog to `normalize_future_assertion`, fully CI-testable) → then prompt v5 worked examples | S → M | Phase 2 only |

[1-object-person-extraction.md](1-object-person-extraction.md) ·
[2-mutual-inverse-edges.md](2-mutual-inverse-edges.md) ·
[3-temporal-relative-resolution.md](3-temporal-relative-resolution.md)

## Dependencies and sequencing

```
Issue 3 deterministic validator ──► ship first (independent, fully CI-able, no prompt bump, no migration)
Issue 1 detection net           ──► ship next  (zero re-extraction cost, surfaces the lapse in logs + xfail)
Issue 1 prompt fix ─┐
Issue 3 prompt fix ─┴─► BATCH into one note-extract-v5 bump (one re-extraction migration, not two)
Issue 1 (object person emitted) ──► PREREQUISITE ──► Issue 2 (inverse edge can only relate two real entities)
```

Three cross-cutting facts drive this order:

1. **Issue 1 gates Issue 2.** You cannot materialize `Celine.spouse → Jeff`
   until Celine reliably exists as an entity. Do Issue 1 first; Issue 2's
   pipeline-derived design then composes on a correct directed edge.
2. **Batch the prompt bump.** Issues 1 and 3 both want a `note-extract-v5`
   prompt revision. Bumping `PROMPT_VERSION` triggers a budgeted corpus
   re-extraction (docs/ANALYSIS.md "Reprocessing"); doing it **once** for both
   fixes halves that cost. Land all deterministic/detection work first, then
   one combined v5.
3. **Deterministic-first beats prompt-first on testability.** All three docs
   independently hit the same wall: the harness *cannot test the prompt* and CI
   never calls a live model. Every fix that can move from "prompt accuracy"
   (untestable in CI) to "deterministic code" (fully unit-tested) is preferred
   — Issue 3's validator and Issue 1's detection net are exactly that, which is
   why they lead. The residual prompt-accuracy gains (Issue 1/3 Phase 2) need a
   small **out-of-CI live-model eval set** as a pre-merge gate — a recurring
   recommendation worth building once and reusing across all three.

## The one risk to read before approving

Issue 2's inverse edge, by definition, writes a fact onto the **object's**
entity stream. If that object is a distinct security subject (Phase 7), naive
auto-materialization attributes a fact **across the domain firewall** — a leak.
The recommended design only auto-writes when the object's `subject_id` is NULL
or equals the source's, routes genuine cross-subject inverses to the review
inbox as proposals, and inherits the source fact's `domain_code` so RLS keeps
each derived edge behind its source's policy. Approve that boundary explicitly.

## Suggested first increment

If you want one shippable PR to start: **Issue 3 Phase 1** (the deterministic
backward-temporal validator + unit tests). It is self-contained, needs no
prompt bump or migration, is 100% CI-testable, directly fixes the "last night"
bug, and establishes the validator pattern Issue 1's detection net reuses.

## Status

Design only — no prompt, schema, pipeline, or test code has been modified this
session. Next step is your call on which increment(s) to authorize.
