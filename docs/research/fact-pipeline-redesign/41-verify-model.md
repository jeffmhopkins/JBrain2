# Final verification — MODEL / EXTRACTION-RELIABILITY lens (consolidated, inline)

Target: `40-final-spec.md` §5. Focus: does the converged reliability posture hold up *at
cold-start adoption*, and did deferring inferred facts break anything everyday?

**No new SEV-1.** Two SEV-2 adoption findings with concrete bootstrapping fixes; two SEV-3.

## Findings

- **V-M1 (SEV-2, the important one) — mandatory plausibility-range coverage would FLOOD review at
  cold-start.** §5 says "no declared range → route to review, not commit." On a fresh corpus
  almost no predicate has a declared range yet → nearly every typed value goes to review =
  adoption-killing. *Fix (bootstrapping — the single most important recommendation):* **invert
  the default** — *no declared range → COMMIT, flagged `type_unverified`* (surfaced for
  opportunistic review, not blocked); only a **declared** range hard-gates (an out-of-range value
  → review). Ranges are added incrementally for the predicates that matter (vitals, money,
  dates), tightening over time. This keeps the "Sam's A1c 5.4, mine 12.8" protection where it's
  declared without taxing every untyped predicate at launch. Update §5.

- **V-M2 (SEV-2) — "modality never model-trusted; cue-less irrealis → review" must NOT capture
  ordinary scheduling.** A future appointment ("dentist next Tuesday", "new job starts in March")
  is a legitimate `expected`/`scheduled` fact with a future `valid_from` — it should **commit
  normally**, not flood review. Only a genuinely **conditional/hypothetical** statement ("*if* I
  switch to Acme", "might move to Denver") holds for review. *Fix:* draw the line explicitly in
  §2/§5: `expected|scheduled` (a real future event with a time) commits; `hypothetical`
  (conditional/uncertain) holds. "Never auto-`asserted`" was always the point — future ≠
  asserted-now — but future ≠ review either.

- **V-M3 (SEV-3) — relative-date resolution is unaffected (confirmed).** "last Tuesday" → instant
  is a deterministic temporal-resolution step in extraction, **not** an "inferred fact"; deferring
  inferred-fact auto-commit does not touch it. The §6 note already says this — good. Everyday
  derived facts a user might miss (age→birth_year) are correctly routed to `add_fact`; acceptable.

- **V-M4 (SEV-3) — co-location should be CLAUSE-level, and ownership named.** Token-adjacency
  co-location risks false-negatives when a correct value is spread across a clause; use a
  clause/dependency window. The plausibility ranges + co-location heuristics are **registry/
  schema-owned**, human-curated, eval-tuned (name this owner in §5 so it's not orphaned).

## Verdict: **SHIP-WITH-CAVEATS.**
No SEV-1. The two SEV-2s are *bootstrapping* fixes (range = commit-flagged-not-block;
scheduled-vs-hypothetical line), not redesigns — fold into §5/§2 before the extraction PR, and
gate them in the eval harness (review-volume budget + the modality confusion matrix).
