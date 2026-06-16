# Fact pipeline & review redesign — Phase 0 framing

**Status:** DRAFT for sign-off (gate G0). Nothing downstream starts until this is approved.
**Owner:** design effort, multi-agent.
**Decision model:** the user gates here (framing) and at the final converged spec; the
fan-out → synthesis → red-team loop runs autonomously in between.

This document frames the problem and the *desired capabilities* — it deliberately does
**not** describe today's implementation or prescribe a solution. Researchers design the
ideal from first principles, then reconcile against the invariants in §4.

---

## 1. Problem statement

A note's prose becomes graph facts (`subject —predicate[.qualifier]→ value-or-object`,
with modality, time, provenance, confidence, domain). Two classes of problem recur:

1. **The sentence → graph-entry contract is lossy and under-specified.** Values arrive as
   whole sentences instead of typed data; entity links, dates, and temporal validity are
   inconsistently captured; the structure the model emits doesn't cleanly carry everything
   a fact needs.
2. **The review/override system is too narrow.** It surfaces a small set of one-off
   decisions (accept / reject / a single value override) rather than letting a human edit
   *every field* of a fact. Two specific failures:
   - **"override vs. array."** Set-valued properties (employers, children, phone numbers)
     are forced through single-value override semantics, so "add another" is impossible or
     silently replaces the head.
   - **"not enough override choices."** Predicate, entity links (subject *and* object),
     dates, temporal precision/recurrence, assertion modality, domain, kind, and cardinality
     are not directly correctable; the human is pushed to free-text correction notes or has
     no path at all.

**Goal:** a first-principles spec for the fact contract *and* the review system in which a
fact is a **structured, fully-editable, possibly multi-valued, bitemporal, provenance-bearing
record**, and review is **structured editing of every field** — with array-vs-single made
explicit — not a fixed menu of overrides.

---

## 2. Corrections wishlist (the capability target)

The new design MUST make every one of these expressible and ergonomic. Researchers should
treat this as the requirements surface (extend it if they find gaps; flag any they argue
should be out of scope).

**Per-field edits on a single fact**
1. **Predicate** — change the relation; pick a known/canonical predicate, map a drift
   spelling, or coin a new one; edit the qualifier (e.g. nickname audience).
2. **Value** — change the literal; typed per the predicate (enum member, quantity+unit,
   date, structured, free text); never forced to a sentence.
3. **Subject entity link** — re-resolve which entity the fact is about (which "Sam").
4. **Object entity link** — for relationships: relink to a different entity, link an
   existing vs. mint a new one, or unlink.
5. **Dates / temporal** — set/edit `valid_from`, `valid_to` (mark former/ended/ongoing),
   precision (instant/day/month/year/era/unknown), recurrence (rrule), and correct
   reported/captured time.
6. **Assertion modality** — asserted / negated / hypothetical / reported / question /
   expected (e.g. "this was hypothetical," "this is negated").
7. **Domain** — move across a firewall (health → general) with the consequences made clear.
8. **Kind** — reclassify a mis-kinded fact (event/measurement/state/attribute/preference/
   relationship).

**Cardinality / arrays (the "override vs. array" core)**
9. For a set-valued predicate: **add** a value, **replace** the current head, or **remove**
   one — explicitly, never ambiguously. The design must define *which* predicates are
   functional (single-valued, supersede) vs. set-valued (accumulate), and surface that to
   the human so the choice between override and add is obvious.

**Structure-level edits**
10. **Split** one extracted fact into several (e.g. "my daughters Summer, Harmony, Lydian"
    → three edges); **merge** several into one.
11. **Add a missing fact** the extractor never produced, via the same structured editor.
12. **Identity ops** — split an entity into two, merge two entities, assert distinct-from.

**Lifecycle / trust**
13. **Drop / retract / supersede** — reject outright, retract as a misread, or supersede an
    older value while keeping history.
14. **Pin / confidence** — pin a human-approved fact so reprocessing can't drop it;
    acknowledge or adjust confidence.
15. **Provenance** — correct the cited source span / note.

---

## 3. The communication-structure question

A central deliverable is the **JSON contract(s)** that carry a fact through its life:
- what the **LLM emits** at extraction (the sentence → structured-fact step);
- the **intermediate representation** integration reasons over;
- the **review payload** a human edits;
- the **edit/operation** representation a human's corrections take.

Open design tensions the research must resolve (not pre-decided here):
- One unified fact shape across all stages, or distinct stage-specific shapes with explicit
  mappings?
- How are **multi-valued** properties represented end-to-end (arrays vs. one-edge-per-value)
  so "add vs. replace" is unambiguous at every stage?
- How are **edits** modeled — a typed operation log (set-field, add-to-set, relink, retime,
  split…) vs. a corrected full record vs. prose? What gives the best audit + reversibility?
- How does the contract **version and migrate** without silent drift?

---

## 4. Invariants (binding constraints — design ideal-first, then reconcile)

These are not design inputs to optimize; any proposal that violates one is rejected.

- **LLM-adapter only** — no provider SDKs in the design's call paths.
- **Storage abstraction** — no raw paths; all persistence through the abstraction.
- **RLS domain firewalls** — health / finance / location isolation enforced in Postgres;
  any new table needs an RLS isolation test. Entity links and edits must never become a
  cross-firewall leak.
- **Bitemporal model** — valid-time (when true in the world) and reported-time (when
  captured) are distinct; the design keeps both.
- **Audit & reversibility** — every committed change is traceable and unwindable (reopen /
  undo), with provenance to a note/span.
- **Machine-written wiki doctrine (#7)** — the wiki is machine-written; humans correct via a
  correction channel, never by direct prose edits. **Tension to resolve, not pre-decide:**
  can structured field edits be modeled as *machine-applied correction operations* (audited,
  reversible) that preserve this doctrine, or does richer review require an explicit, bounded
  doctrine change? The research must take a clear position and the red-team must attack it.
- **Conventional Commits, branch+PR, CI-green, tests-with-code** — process constraints on
  the eventual implementation, noted so the spec stays buildable.

---

## 5. Success criteria

The converged spec is "solid" when:
- Every §2 wishlist item is expressible, and "override vs. add (array)" is explicit and
  ergonomic for set-valued predicates.
- The LLM contract is **reliably emittable** (schema-constrained), **validatable**
  (deterministic backstops for what the model omits), **versioned**, and **migratable**.
- RLS/firewalls preserved; every edit path is reversible and audited; the #7 doctrine
  position is explicit and defended.
- The number of bespoke review "kinds" and one-off override prompts goes **down**, replaced
  by a unified editable-fact model.
- It survives the red-team: a full review round produces no new Sev-1/Sev-2 finding.

---

## 6. Research tracks (Phase 1 fan-out — "deep")

Independent, greenfield briefs; each returns: proposal, strawman schema/IR, rationale,
tradeoffs, risks, open questions.

- **A · Fact intermediate representation** — the in-flight fact shape(s) the model emits and
  integration reasons over: typed values, links, modality, confidence, provenance.
- **B · Storage & graph model** — how facts persist: bitemporal intervals, multi-valued /
  functional predicates, supersession & history, provenance, identity.
- **C · Correction taxonomy & "array vs. override"** — the full edit-operation algebra; the
  #7 doctrine reconciliation; reversibility/audit; functional-vs-set semantics surfaced.
- **D · Prompt & extraction reliability** — eliciting the new JSON dependably; schema-
  constrained output; deterministic validators/backstops; contract versioning + eval.
- **E · Review architecture & UX** — collapsing the review-kind zoo into one editable fact
  record; card-as-structured-editor with per-field overrides + arrays; batching; audit trail.
- **F · Security, RLS & domain firewalls** — how links, relinks, and domain moves stay
  firewall-safe; isolation-test obligations; injection/abuse surface of richer edits.
- **G · Temporal & recurrence** — valid-time intervals, precision, "former/ongoing,"
  recurrence (rrule), and how edits to time stay sound bitemporally.

## 7. Red-team lenses (Phase 3 — iterate to convergence, cap ~5 rounds)

Each round, distinct adversarial lenses attack the current spec revision; findings graded
Sev-1 (must-fix, breaks an invariant or a core goal) … Sev-3 (nit). Loop until a full round
adds no Sev-1/2; residual issues become documented accepted-risks / open questions.

- Correctness & edge cases (negation, hypotheticals, multi-valued, conflicting temporals,
  splits/merges).
- Model-compliance & reliability (will the LLM actually emit this? hallucinated links,
  partial output, schema drift).
- Security / RLS / firewall leakage (links, relinks, domain moves, injected edits).
- Migration, back-compat & reversibility (existing facts, re-analysis, audit, undo).
- Over-engineering & ergonomics (contract complexity, cognitive load, the override-vs-array
  UX, fewer-kinds claim).
- Performance & scale (contract size, per-fact validation/embedding cost, batch review).

## 8. Scope

**In:** the extraction → integration → storage fact contract(s); the review-item model and
edit-operation algebra; prompt deltas; a phased migration/rollout sketch.
**Out (noted as dependencies, not designed here):** the production UI build; model/provider
selection; non-fact surfaces (search, wiki rendering, calendar) except where they consume
facts. **No code is written in this effort** — implementation is planned separately after
sign-off of the final spec.

---

## 9. Deliverables & artifacts

Committed under `docs/research/fact-pipeline-redesign/`:
- `00-framing.md` (this doc).
- `10-research-*.md` — the seven track briefs.
- `20-spec-v0.md` … `2N-spec-vN.md` — synthesis revisions.
- `30-redteam-round-*.md` — red-team logs per round.
- `40-final-spec.md` — the converged spec + migration plan + decisions/open-questions.
