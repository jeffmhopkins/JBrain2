# Spec vs. current implementation — comparison & plan-change findings

The research was deliberately greenfield (it never read `backend/src`). This reconciles the
final spec against what is **actually shipped**. **Headline: the current system already
implements ~75–80% of the spec's substance.** A clean-rebuild-to-greenfield (decision D1, as
literally written) would discard a mature, tested, RLS-hardened, registry-backed, bitemporal
system to rebuild most of it. The recommended pivot is **incremental evolution**, cherry-picking
the genuine wins. **Findings only — no plan change made yet; decide below.**

---

## 1. What the current implementation ALREADY has (the surprise)

From `models/analysis.py`, `analysis/supersession.py`, `analysis/repo.py`, the schema registry,
and the work we shipped this session:

| Spec proposal | Already in the current system? |
|---|---|
| Bitemporal facts (valid_from/to + reported_at + precision) | ✅ `Fact.valid_from/valid_to/reported_at/temporal_precision` |
| Modality | ✅ `Fact.assertion` (asserted\|negated\|hypothetical\|reported\|question\|expected) — **as a column** |
| Supersession-chain revision history (append-mostly) | ✅ `Fact.superseded_by` + `status`; "never hard-deleted by app code" |
| **Cardinality / override-vs-array at storage** | ✅ `is_functional(predicate)` (registry flag) → functional supersedes, set-valued **accumulates**; `values_equal` (object_entity_id / value_json) is the member identity |
| One-edge-per-value for relationships | ✅ each child/employer is its own `Fact` with its own `object_entity_id` |
| Predicate registry + canonicalization + value_shape + enum coercion + plausibility | ✅ `canonical_predicates` (embeddings), STRONG/WEAK bands, `coerce_value`/`validate_value` |
| Deterministic value recovery (no sentence-as-value) | ✅ shipped this session (`_shape_check` recovery + prompt v19) |
| Entity merge reversibility + distinct-from + span mentions (re-resolution) | ✅ `merged_into_id`, `EntityDistinction`, `EntityMention` |
| Pinned human-override facts (survive reprocessing) | ✅ `Fact.pinned` |
| RLS domain firewalls (health/finance/location) + isolation tests | ✅ `domain_code` + scoped sessions; every table |
| Cross-subject attribution separation | ✅ `Fact.subject_id` / `Entity.subject_id` (a security-subject distinct from entity) |
| Review inbox + the #7 correction-note channel | ✅ `review_items`, `resolve_review`, correction notes |
| Human per-field editing of a held fact (predicate + value) | ✅ shipped this session (PRs #234/#236) |
| Scheduled re-analysis / migration engine + run-log | ✅ Phase-5 workflow engine |
| Undo of a review decision | ✅ `reopen_review` reverses recorded resolution **effects** |

The spec's central "cardinality in the identity key" insight is **already realized**, just
expressed in the *decision logic* (`is_functional` + `values_equal`) rather than a hashed key.

---

## 2. The genuine deltas (what the spec actually adds) — good & bad

**A. Modality (assertion) is NOT in the selection key / `current()` filter.** The documented
identity is `(subject_id, entity_id, predicate, qualifier)` — value and assertion excluded. So a
`negated` "not allergic to penicillin" and an `asserted` "allergic" share a key and can collide,
and non-asserted modalities may leak into the "current" view. **GOOD:** the spec's modality-in-
selection-key + `current()=asserted-only` is a *real correctness fix* (health-safety). **Cost:**
a supersession-logic + index change. **→ Worth doing.**

**B. General op-log + arbitrary-order undo.** Current undo = `reopen_review` reversing recorded
review-decision *effects*; the fact-revision history is the supersession chain. There is **no
general op-log**, so "undo any change, any order" (your Decision 3) is *not* achievable today.
**GOOD:** the spec's selective-replay op-log is the one thing current genuinely can't do. **BAD:**
it's the single largest new subsystem; it duplicates some of what supersession-history already
provides. **→ Worth doing *only if* arbitrary undo is a real requirement; scope it as an additive
layer over the existing append-mostly history, not a rewrite.**

**C. Structured-editing review (collapse the kind-zoo into one op-submitting card).** Current is
the kind-zoo + correction notes + the per-field editing we just added. **GOOD:** fewer bespoke
cards, every field editable, explicit arrays — directly serves your original ask. **Neutral:** it
*evolves* the review UI we already started, not a rebuild. **→ Worth doing incrementally** (and it
triggers decision D2: 3 mockups first).

**D. Per-domain entity projections (vs current global tables + RLS).** **BAD / likely over-
engineering.** Current is a single global `entities`/`facts` set with `domain_code` + RLS, already
isolation-tested. The spec's projections defend against a Postgres FK-covert-channel and a relink
read-oracle — real in a *multi-tenant* setting, but this is a **single-user** system where the
owner is authorized across all their own domains. The projection model is a large schema +
resolver complexity bomb (the red-team's own residual high-value asset) for a threat that's
marginal here. **→ Recommend NOT adopting; keep global tables + RLS, harden the specific leaks if
any (the relink firewall check we already gate).**

**E. Two-stage extraction (candidate → type+link).** Current is single-extract + a separate
integration/arbiter pass that already injects the registry for canonicalization. **Mixed:** the
spec's two-stage improves typed-value/link grounding but doubles LLM cost and is a pipeline
rewrite. The deterministic backstops (the valuable part) we **already added** this session. **→
Recommend keeping single-stage + the backstops; adopt two-stage only if eval shows a real
grounding gain.**

**F. Stable `value_identity` for scalar set members.** Current uses `values_equal` (value_json
equality) as member identity. **GOOD (marginal):** a stable minted id makes "typo-fix vs add"
cleaner for scalar sets (the red-team's member-drift findings). **→ Small, do it with the
cardinality work if/when touched.**

**G. TypedValue discriminated union (5 variants).** Current uses freeform `value_json` + the
registry `value_shape` + coercion. **Mixed:** the union is cleaner/safer but a contract+migration
change; current works with the validator we have. **→ Low priority.**

---

## 3. Where the spec is *worse* than current (don't regress)

- **D1 "clean rebuild" as an architecture wipe** — current is mature and tested; rebuilding ~80%
  greenfield is high-risk for little gain. (D1 as a *data* operation — re-ingest notes — is fine.)
- **Per-domain projections** — strictly more complex than the working RLS model (§2.D).
- **Two-stage extraction** — more cost/complexity than the working single-stage + backstops.
- The current `derived_from_fact_id` (materialized reciprocal edges), `subject_id` security
  separation, `is_schedule_binding` / inverse-predicate handling, and the residual functional
  allowlist are **shipped nuance the spec doesn't mention** — must not be lost in any change.

---

## 4. Recommended plan change (for your decision — NOT yet applied)

**Pivot from "clean rebuild to the greenfield spec" → "incremental evolution of the existing
system, adopting the spec's genuine wins."** Concretely:

**Adopt (real wins, incremental):**
1. **Modality in the selection key + `current()` = asserted-only** (negation safety) — supersession
   + index change. *High value, contained.*
2. **Structured-editing review** — evolve the review card we started into the unified op-submitting
   editor; collapse kind-zoo opportunistically. *(D2: 3 mockups first.)*
3. **Arbitrary-order undo** *(your Decision 3)* — add a typed-op/audit layer over the existing
   append-mostly history for selective-replay undo, **scoped as an addition**, not a storage
   rewrite. Re-confirm you want full arbitrary undo vs. extending today's reopen-effects.
4. **Stable `value_identity`** for scalar set members — small, with the cardinality work.
5. Keep the **deterministic backstops, registry, bitemporal, RLS, pinned, merge/mentions** as-is.

**Drop / defer from the spec:**
- **Per-domain entity projections** — keep global tables + RLS (single-user; over-engineered).
- **Two-stage extraction rewrite** — keep single-stage + backstops unless eval proves a gap.
- **Full TypedValue-union contract migration** — low priority; the validator covers it.

**Re-scope D1:** "complete DB reset" = **re-ingest notes** under the improved pipeline (fine and
useful), **not** an architecture rewrite. Most upgrades are online schema migrations on the
existing tables.

**Net:** this delivers everything you actually asked for (edit every field, explicit override-vs-
array, more override choices, arbitrary undo, structured review) by **upgrading** a working system
in small CI-green PRs — far less risk than a greenfield rebuild, and it preserves the shipped
nuance.

---

## 5. Decisions for you

1. **Architecture posture:** incremental evolution (recommended) vs. clean-rebuild to the
   greenfield spec?
2. **Per-domain projections:** drop (recommended) vs. adopt for firewall hardening?
3. **Two-stage extraction:** keep single-stage + backstops (recommended) vs. rewrite?
4. **Arbitrary undo:** confirm it's a hard requirement (→ build the op/audit layer) vs. "good
   enough" is extending today's reopen-effects + supersession history?
5. **D1 meaning:** re-ingest-only (recommended) vs. full rebuild?

If you accept the recommended posture, I'll update the final spec + decisions log accordingly and
sequence the incremental PRs (backend first; the review GUI gated by D2's mockups).
