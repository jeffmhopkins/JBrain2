# Red-team R2 — Model-compliance & extraction reliability

**Lens:** MODEL-COMPLIANCE & EXTRACTION RELIABILITY (Round 2).
**Target:** `21-spec-v1.md` (revision after R1). Inputs: `00-framing.md`,
`30-redteam-r1-model.md` (R1 findings being verified).
**Status:** adversarial findings for the convergence loop. Sev-1 = breaks an
invariant or a core goal (commits a wrong fact, leaks a firewall, or floods review
so the system is unusable); Sev-2 = reliably wrong on a real input class; Sev-3 = nit.

**Through-line, unchanged from R1 and still the root cause in v1:** v1 added real
oracles (registry ranges, closed inference verifiers, co-location). But an oracle is
only as good as (a) its *coverage* — a predicate with no range, or a range too wide,
silently reverts to "parser+model agree = commit"; and (b) its *premise integrity* —
a verifier that checks an *operation* (arithmetic, co-location, finish_reason) does
**not** check the *truth of its inputs*, all of which are still attacker-controllable
prose. v1 closes the specific R1 exemplars (A1c-vs-glucose, free-form traces) but the
*classes* survive wherever the new oracle's coverage is partial or its premise is
prose-derived. Two such gaps are Sev-1.

---

## PART 1 — Verification of R1 findings

| R1 finding | v1 disposition | R2 verdict |
|---|---|---|
| SEV-1.1 parser+model agree on wrong span | B2b range + co-location + unaligned zero-tol eval | **PARTIALLY FIXED — see R2 SEV-1.1.** Closed for distinct-predicate disjoint-range case; OPEN for same-predicate multi-value and absent/wide-range predicates. |
| SEV-1.2 inferred derivation-trace forgeable | closed verifier templates from literal spans | **PARTIALLY FIXED — see R2 SEV-1.2.** Free-form trace channel closed; attacker-stated false *premise* still verifies. Commit-vs-review of verified-inferred is unspecified. |
| SEV-1.3 modality trusted, lexicon not an oracle | health/finance any-hit-or-low-conf → review; span-quoted re-ask; clinical slice | **FIXED for health/finance. OPEN for general domain — see R2 SEV-2.1** (cue-less irrealis still commits asserted outside the firewall). |
| SEV-1.4 cross-field invariant not decode-guaranteed | R5 bi-conditional both directions; committer re-derives kind from value_shape; structural-coherence metric | **CONFIRMED FIXED.** Re-derivation from registry value_shape removes the model's `kind` from the trust path; coined/unknown-predicate (no value_shape) routes to review, so no silent un-typed re-derivation. |
| SEV-2.1 registry mis-flag silently supersedes | additive-cue conflict → review; low-conf flag defaults to set | **CONFIRMED FIXED** for cued adds. Residual nit only: a *non-cued* true second member on a mis-flagged-functional predicate ("my number is 555-0002" with zero additive cue) still supersedes — narrower than R1, downgraded to SEV-3.3. |
| SEV-2.2 re-ask injection + truncation commit | closed-enum error codes; finish_reason hard-reject; per-note re-ask bound | **PARTIALLY FIXED — see R2 SEV-2.2.** Injection-oracle and truncation closed; **non-truncated optional-field omission** (same data-loss outcome) is NOT caught by finish_reason. |
| SEV-2.3 coined-predicate / shape wrong-merge | review band below threshold; closed structured set; registry-owned threshold + wrong-merge eval | **CONFIRMED FIXED** in mechanism. Residual: the band width / threshold owner is explicitly still-open-tuning (§7 own admission) — accepted as tuning, not a blocker. |
| SEV-2.4 over-extraction flood + mint dup | Stage-1 deterministic precision gate; provisional flagged mint + deferred dedup | **CONFIRMED FIXED** in mechanism. The precision gate itself is prose-derived (subject+relation+typeable-object heuristic); its false-negative rate is an eval curve, not an oracle — acceptable, watched by the precision watch-metric. |

**Summary:** SEV-1.4 fully confirmed; SEV-2.1/2.3/2.4 confirmed (with shrunk
residuals). SEV-1.1, SEV-1.2, SEV-1.3, SEV-2.2 are each only *partially* fixed —
the named exemplar is closed but the underlying class re-appears through a coverage
or premise gap, escalated below.

---

## PART 2 — New / re-opened findings against v1's reliability mechanisms

### SEV-1.1 (R2) — B2b range + co-location have NO oracle for *which* in-range co-located value is correct, and NO mandatory range coverage, so the "agree-on-wrong-value" commit survives for same-predicate multi-value clauses and any absent/wide-range predicate

**Where:** §2.2 B2b ("checked against the *fact's* predicate range"); §5.2-B; §7c
("SETTLED"); §7 still-open admits the range/threshold is unowned-tuning.

**The break (two coverage gaps the R1 fix does not reach):**

1. **Same-predicate, both-in-range, both co-located.** B2b discriminates A1c (3–20)
   from glucose (40–600) because the ranges are *disjoint across predicates*. It does
   nothing when two values of the **same** predicate sit in adjacent clauses, because
   both pass the same range and co-location is satisfied for both. The parser has no
   oracle for *which* number pairs to *which* subject/time.

2. **Absent or wide range = silent revert to R1 behaviour.** The spec assigns range
   ownership to "registry config" and lists range/threshold tuning as *still open*. It
   never states a range is **mandatory** per quantity/date predicate, nor an eval gate
   on range *correctness/tightness*. A registry quantity-predicate shipped with no
   range, or a deliberately-safe wide range, makes B2b a pass-through — and the exact
   R1 SEV-1.1 "parser+model agree on a wrong number, in-range, parses cleanly" commit
   returns, now *believed fixed*.

**Concrete failing input (gap 1, health firewall):**
*"Sam's A1c was 5.4; mine was 12.8."* Stage-2 candidate for `health.a1c`,
subject=self (me). Model anchors the value to the **wrong clause**: emits
`value:5.4` for the self fact. B1: "5.4" is in the cited span. B2: parses. B2b: 5.4 ∈
[3,20] → **pass** (it is a perfectly plausible A1c). Co-location: "5.4" sits in a
clause containing the `A1c` cue → **pass**. Committed: **my A1c = 5.4** when the note
says 12.8 — a clinically inverted (normal-vs-diabetic) health measurement, high
confidence, no review. Every new oracle passes because the wrong value is itself a
valid A1c; the only thing wrong is *which* A1c, and nothing checks that.

**Concrete failing input (gap 2):** any registry predicate added later with no
plausibility range (e.g. a new `finance.account_balance` whose range is left null
because balances span many orders of magnitude). Model misreads "$4,200 / acct ending
4200" and commits `4200` as the balance. B2b has no range → passes. R1 SEV-1.1 reopened
for that predicate, undetected because the eval golden set is frozen and predates it.

**Why the eval still doesn't catch it:** the §5.4 "committed-value-error scored even
when unaligned" gate fixes the *alignment-hiding* problem but only for facts in the
golden set. A same-predicate adjacent-value swap produces a fact that *aligns to the
wrong gold row of the same predicate* (right predicate, right subject set, wrong
pairing) — bipartite alignment by (subject, predicate, span-overlap) can still match
it to *a* gold A1c and score it inside tolerance if both values are A1c-shaped.

**Fix:** (a) Make a plausibility range (or an explicit `range:none` waiver that
*forces review* for that predicate's quantities) a **mandatory, schema-required
registry field** for every quantity/date predicate, with a CI gate that fails if a
quantity predicate ships without one; assign a named owner (registry maintainer) and
an eval metric on range tightness. (b) For **same-predicate multi-value clauses**, add
a "multiple typeable values of the same predicate within the retrieval window" detector
that routes the whole cluster to a **disambiguation review** (which number → which
subject/time) rather than letting the parser pick — the parser must not silently
resolve a value↔subject/time pairing it has no oracle for. (c) Eval slice: notes with
≥2 same-predicate values in adjacent clauses (mine-vs-theirs, today-vs-yesterday),
zero-tolerance on cross-pairing.

---

### SEV-1.2 (R2) — closed inference verifiers check the *operation*, not the *premise*; an attacker-/error-stated false literal premise satisfies the verifier and (absent an explicit inferred→review rule) commits a confidently wrong derived fact

**Where:** §5.2 "Closed inference templates"; §7d ("SETTLED … each with a
deterministic verifier that recomputes the value from cited literal spans").

**The break:** the R1 fix narrowed *which operations* are admissible (arithmetic-only,
closed set) and demanded the inputs be *literal spans* — this kills the free-form
"trust my reasoning" trace. But the verifier's guarantee is **"the arithmetic from the
cited literals is correct,"** NOT "the cited literals are true." Every literal premise
(`stated_age`, `anchor_date`, `offset`) is still extracted from the same untrusted
prose. The verifier recomputes `2026 − 40 = 1986` and certifies it; it has no way to
know "40" was attacker-supplied or a misread of "4.0". So a fabricated/false premise
produces a *deterministically verified* wrong fact — the verifier launders it from
"model guess" to "machine-checked," which is *worse* for reviewer anchoring than an
honestly-unverified value.

Compounding gap: **v1 never states what terminal state a verified-inferred fact lands
in.** v0 routed inferred to review; §7d/§5.2 in v1 describe only the verifier and a
per-note cap. If a verifier *pass* now **auto-commits** (the natural reading of
"admissible via verifier"), there is no human in the loop on a false-premise inference.
This is an unresolved spec ambiguity that, on the auto-commit reading, is Sev-1.

**Concrete failing input:** Note (or injected note span): *"Patient turned 90 last
week."* Template `age_to_birthyear{anchor_date=2026-06-16, stated_age=90}`, both cited
to literal spans ("90", capture date). Verifier: `2026 − 90 = 1936` ✓ recomputed from
literals → **verified**. If verified-inferred auto-commits, `birth_year = 1936` is
committed with elevated trust on a fabricated age. The arithmetic is flawless; the
premise is a lie; no oracle checks the premise. (`relative_date` is worse:
*"moved here right after my 1.0-year-old started walking"* → a misparse of a literal
into a plausible offset still verifies.)

**Fix:** (a) State explicitly that **verified-inferred facts route to review (or
candidate floor), never auto-commit** — the verifier downgrades confidence and labels,
it does not grant commit authority; make this a tested terminal-state rule, not prose.
(b) Apply B2b's **plausibility range to the inferred *output*** (birth_year ⇒ derived
age ∈ [0,120]; a derived future birth_year fails) so an out-of-band false premise is
caught even pre-review. (c) Require the premise literal itself to pass a *premise-side*
plausibility gate (stated_age ∈ [0,120]) before the template runs. (d) Eval: an
injection slice with **true-arithmetic / false-premise** inferences (not just forged
traces — R1's slice) with zero-tolerance on auto-commit.

---

### SEV-2.1 (R2) — modality remains model-trusted in the GENERAL domain; cue-less irrealis commits an `asserted` future fact outside the firewall, bypassing the §3.3 promotion machinery

**Where:** §5.2-B3 ("**health/finance**: ANY lexicon hit OR low-conf ⇒ review");
§3.3 (expected/scheduled don't auto-flip); R1 SEV-1.3 fix (domain-gated).

**The break:** the R1 modality fix is explicitly scoped to **health/finance**. For the
general domain, modality is still the model's free choice gated only by the lexicon the
spec itself calls noisy. The §3.3 candidate-floor / explicit-promotion machinery is
sound **only if the row was correctly labeled `expected`/`hypothetical`**. A cue-less
plan that the model mislabels `asserted` never enters the candidate floor at all — it
is asserted-live with a future `valid_from`, and the promotion gate (which only acts on
non-asserted rows) never engages.

**Concrete failing input:** *"Switching to Acme in January."* (general domain,
`person.employer`; no "if", no "would", no "planning to"). Model emits
`modality:asserted, valid_from:2026-01`. B3 lexicon finds no conditional marker → does
not fire. Not health/finance → no mandatory review. Committed: an **asserted** future
employer. Because it is asserted (not `expected`), §3.3's "no auto-flip" never applies
— it's simply live with a future start, and `current()` will surface it the moment
`now` crosses January, with no realization op ever required. The R1 fix's own §2.6(iv)
exemplar assumed the cue "if" was present; this input omits it.

**Fix:** (a) Extend the modality-review gate beyond health/finance: any
`person.employer`-class **functional, future-`valid_from`, asserted** fact with no
asserting present-tense cue routes to review (a future-dated *asserted* fact is itself
the anomaly, independent of domain). (b) The span-quoted modality re-ask (already built
for health/finance) should fire for **any future-dated asserted fact** — require the
model to quote the words establishing it is already-true vs planned. (c) Eval: cue-less
irrealis slice in the general domain ("starting Monday", "moving next month").

---

### SEV-2.2 (R2) — finish_reason guards *truncation* but not *non-truncated optional-field omission*; the identical R1 data-loss outcome (dropped exdate, dropped valid_to) commits with finish_reason=stop

**Where:** §5.2-A ("completeness/truncation check"); §5.1 ("truncation detected via
finish_reason"); R1 SEV-2.2 Side-B fix.

**The break:** R1 SEV-2.2 Side-B was "a truncated object that still parses commits a
partial fact (dropped `exdates`, dropped `valid_to`)." v1 fixes the *truncation* path:
`finish_reason != stop` → hard reject. But the **same harmful output** arises without
truncation: the model emits a syntactically and semantically *complete, un-truncated*
object that simply **omits an optional field it should have populated** — drops
`valid_to`, drops `exdates`, drops `valid_from.bound`. finish_reason is `stop` (the
model finished cleanly), A1 passes (the fields are optional), and the recurrence /
interval commits with the exact loss R1 flagged. finish_reason detects *running out of
room*, not *choosing to omit*. Worse: with **constrained decoding**, some backends
close the JSON at the grammar's accept state when the token budget is hit, surfacing
`finish_reason=stop` even though emission was cut short — so the truncation guard can
also *miss* the truncation case it was built for, depending on adapter semantics.

**Concrete failing input:** A note with *"PT every Tue/Thu, but not Sept 8."* Stage-2
emits a complete `recurrence` object: `rrule`, `dtstart`, `tz`, `count_cap`, but
**omits `exdates`** (model didn't connect "not Sept 8" to an exclusion — a semantic
miss, not a length cutoff). finish_reason=stop; `exdates` optional → A1 passes;
committed recurrence has no exclusion → the system asserts a therapy session on the
date the note explicitly excluded. Identical to R1's exemplar, via a path finish_reason
cannot see.

**Fix:** (a) Add a deterministic **negative-cue → required-field** check: an exclusion
cue in the span ("but not", "except", "skip") with no `exdates`, or a closure cue
("until", "till", "ended") with no `valid_to`/closed bound, is a **completeness review**
(the field the prose demands is absent). This is the independent oracle finish_reason
lacks. (b) Pin down the adapter contract: `finish_reason` semantics under constrained
decoding must be specified (length-hit-mid-grammar must surface as non-`stop`), with an
adapter conformance test — otherwise the truncation guard's reliability is
backend-dependent. (c) Eval: a slice pairing an explicit exclusion/closure cue in prose
against a complete-but-field-omitting emission, asserting it never silently commits.

---

## PART 3 — SEV-3 (nits / accepted-risk candidates)

- **SEV-3.1 — B2b range ownership and the canonicalization threshold are *both*
  unowned-tuning (§7 admits this).** Acceptable as tuning, but two safety-critical
  numeric knobs (plausibility ranges, merge-band width) with no named owner and no
  correctness eval gate is a process gap; fold range-coverage into the same
  registry-config CI gate proposed in SEV-1.1(a).
- **SEV-3.2 — non-cued true second member on a mis-flagged-functional predicate still
  silently supersedes.** R1 SEV-2.1's cue-based guard only fires on an additive cue;
  "my number is 555-0002" (no "also"/"another") on a mis-flagged-functional
  `person.phone` still supersedes 555-0001. Mitigated (low-conf flags default to set;
  member-stability), so narrower than R1, but not zero. Mitigate further per R1's own
  unused suggestion: supersession of a recent live member with a *different
  value_identity natural key* is a soft review trigger.
- **SEV-3.3 — calibration curve provenance.** §5.2-F asserts per-domain curves "no
  cross-firewall leak" but the curve-fitting *data partition* and its versioning still
  aren't in PROCESS; keep R1 SEV-3.2 open as a process item, not a blocker.

---

*End R2 (model-compliance lens). Net: SEV-1.4 and the SEV-2.x mechanisms hold; but
B2b and the inference verifiers fix their R1 *exemplars* without closing the R1
*classes* — both have an unguarded premise (which in-range value? is the stated age
true?) that is still prose-derived, and the truncation guard fixed truncation but not
omission. Same root cause as R1: a new oracle that checks an operation, not its
prose-sourced inputs, and whose coverage is partial, lets a reproducible wrong value
through — and a reproducible wrong value still passes the ablation test.*
