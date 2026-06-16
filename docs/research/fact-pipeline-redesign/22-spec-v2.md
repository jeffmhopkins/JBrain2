# Fact pipeline & review redesign — spec v2 (red-team-hardened, round 2)

**Status:** revision of `21-spec-v1.md` after Red-Team Round 2 (six lenses). Records the
**deltas and settled decisions**; for unchanged detail (the `TypedValue` union now at §2.2,
the `temporal` object §2.6, worked examples §2.7, the full isolation-test list §6, the
extraction stages §5.1–5.2) **v1 still stands** except where overridden here. §11 is the
Round-2 disposition table (every R2 Sev-1/2 → resolution). §10 lists the few decisions still
genuinely open for the user.

**Round-2 verdict:** v1's design held; six second-order mechanism gaps were found and the
lenses converged on the fixes. v2 is **smaller** than v1 in several places (one fewer
materialized table, 5 value variants not 7, inferred-fact auto-commit removed) and stricter
where it was unsafe. No foundational change — the spine of v0/v1 stands.

---

## 1. Revised spine (corrections to v1 §1)

- **Reversibility is now stated HONESTLY (amends framing §4 — see §8).** Undo is
  snapshot/state-based with **cascade-or-block**; *removing an earlier op's effect while
  keeping a later dependent op is a NEW forward correction, not an undo.* This is a **bounded
  amendment to the §4 "every change is unwindable" invariant**, on the same footing as the #7
  doctrine reconciliation — surfaced, not silently assumed. (migration R2-1; ergonomics N5.)
- **No separate authoritative `fact_current` table.** Current-value is served by the **existing
  partial unique index** on `live_key WHERE tx_to IS NULL AND modality='asserted'` — an indexed
  lookup over the append-only assertions, **not a second source of truth dual-written every
  op.** (ergonomics N1 + performance R2-N1 converge.)
- **Keys bind a STABLE `predicate_id`, never the mutable `predicate.canonical`**, so predicate
  re-canonicalization is a registry pointer change, not an O(history) bulk re-key. (perf R2-N2.)
- **Inferred-fact auto-commit and the inference-template registry are DEFERRED** out of the
  core (a future feature). An inferred derivation routes to human `add_fact`/review; nothing
  ungrounded auto-commits. (model SEV-1.2 + ergonomics N4 — dissolves the premise-verification
  SEV-1.)

Everything else in v1 §1 stands (model-proposes/committer-decides; append-only op-log; two
identity keys; #7 preserved; bitemporal typed facts).

---

## 2. Contract deltas (vs v1 §2)

- **`TypedValue` → 5 variants** (ergonomics F7): `enum`, `quantity`, `date`, `text`, `ref`.
  `boolean` folds into `enum` (a 2-member closed set); `structured` is built on demand (its
  only near-term member is `address`) and added back as a 6th only when a 2nd structured shape
  appears. The 1:1 contract↔registry `value_shape` property **still holds at 5** (the registry
  gains no shape these don't cover). Update §2.2 accordingly.
- **Modality is never model-trusted** (model SEV-2.1/1.3): cue-less future/irrealis ("switching
  to Acme in January") → **low confidence + review, never auto-`asserted`**, in every domain
  (not just health/finance). The B3 lexicon cross-check feeds confidence; modality is only
  `asserted` on the live floor via extraction with an explicit cue OR a human/`realize` op.
- **Inferred provenance kind**: retained in the schema but **non-auto-committing** (see §1);
  an `inferred` claim always routes to review/`add_fact`. §7d settled = deferred feature.

---

## 3. Storage & identity deltas (vs v1 §3)

- **`value_identity` uniqueness rule** (correctness R2-1): a **per-`(slot, natural_key)` UNIQUE
  constraint**. Priority: `object_id` (ref) → declared natural key (E.164 phone, lowercased
  email, normalized name) → a minted member-id **only when no natural key exists**. A
  mint-race on the same natural key resolves by **deterministic idempotent merge on that key**
  (the UNIQUE constraint makes the second insert a no-op/merge, never a fork). Minted ids are
  carried forward by supersession and **healed on entity-merge**.
- **Per-member history** (correctness R2-4): history is reconstructed **per `value_identity`'s
  own supersession chain** — each member is its own interval series. `current()` returns the
  live row per `live_key`; `history(slot)` returns per-member chains, **never a naive
  union-by-`identity_key`** that would collapse `[2019–2023]+[2025–open]` across a gap.
- **Keys via a single `keys(fact)` function** (ergonomics N2) over a **stable `predicate_id`**
  (perf R2-N2): `identity_key` (predicate_id, qualifier, subject, domain, modality,
  value_identity) and `live_key` (same minus value_identity for functional; minus modality-non-
  asserted from the live floor). One function encapsulates all three identity concepts; nothing
  else recomputes keys.
- **No `fact_current` table** (§1): drop v1 §3's materialized projection; keep the partial
  unique index as the current() access path; partition live/archive for as-of history; suppress
  no-op reprocess writes.
- **Former without auto-abutment, reconciled with the functional live-index** (correctness
  R2-5): marking a prior value `former` sets `valid_to.bound=unknown` **in valid-time only** and
  is a **tx-supersession** (`tx_to` set, `state='superseded'`) — these are two axes, never
  double-specified; the functional unique index keys on `live_key` + `tx_to IS NULL`, so a
  former (superseded, `tx_to` set) row is already out of the live set and cannot collide.

---

## 4. Correction algebra deltas (vs v1 §4)

- **`realize` op DEFINED** (correctness R2-3): `realize(fact, to_modality='asserted')` supersedes
  the `hypothetical`/`expected` row with an `asserted` row on the **same `value_identity`** (a
  modality re-key). It is the *only* path from irrealis to the live floor; **nothing
  auto-promotes by wall-clock** (the realization is an explicit human or rule-driven op).
- **Set-predicate contradiction check** (correctness R2-2): on commit/review, a **modality-
  stripped comparison** flags an `asserted`+`negated` pair on the same
  `(subject, predicate_id, value_identity)` → review (the distinct-`live_key` blind spot is
  closed by a dedicated contradiction index that ignores modality).
- **`merge_facts` hardened** (correctness R2-6): provenance is **unioned, never dropped**;
  merging **across modality is rejected** (no `asserted`-from-`negated` laundering); cross-domain
  merge rejected; conflicting typed values → review.
- **Snapshot-undo correctness** (correctness R2-7, migration R2-2, security NEW-4): on
  un-tombstone the committer **(i)** re-checks `live_key` slot occupancy — if occupied, the undo
  is surfaced as a **forward-correction conflict to review**, never an attempted insert that
  throws the unique index; **(ii)** **re-derives** protection metadata (`human_touched`,
  `certainty`) **and `domain`** from current topology — a frozen `domain_code`/flag is never
  resurrected (closes the firewall-bypass on un-tombstone); **(iii)** routes a **note-
  contradicting** undo to review.
- **`domain_move` = PUBLISH** (security NEW-1/NEW-3): a downgrade is a deliberate, owner-
  confirmed **publish** that is **effectively irreversible in the security sense** — anything
  derived from the published general copy is already public. "Undo" = **retract (tombstone, not
  destroy/purge)** the general copy + audit; it **cannot un-publish derivations**. The "purge on
  undo" of v1 is **removed** (it was a destructive footgun and didn't actually un-leak).
  Derivation-before-undo (NEW-1) is **accepted as inherent to publishing** — bounded by:
  rare, owner-only, audited in both bands, and the **owner is told the publish is irreversible**.
- **Undo human surface** (ergonomics N5): "**undo last**", "**revert to point**", "**correct
  instead**" — the cascade dependency graph and the three-key internals are **never exposed** to
  the reviewer; a blocked undo offers "correct instead" (a forward op), not an internals dump.

---

## 5. Extraction & reliability deltas (vs v1 §5)

- **Typing oracle hardened** (model NEW-1/SEV-1.1): (i) **plausibility-range coverage is
  mandatory** — a predicate with no declared range routes its values to **review, not commit**
  (no silent absent-range fallback); (ii) **subject+predicate+value CO-LOCATION** — the typed
  value's span must co-locate with **both** its subject mention **and** the predicate cue,
  catching cross-subject capture ("Sam's A1c 5.4, mine 12.8" no longer commits the wrong A1c).
- **Field-omission guard** (model SEV-2.2): the validator requires **structural completeness** of
  required sub-objects (e.g. `exdates`/`valid_to` when `recurrence`/closed interval implied) —
  present-or-review, **independent of `finish_reason`** (a non-truncated response that simply
  omitted a field is caught).
- **Inferred facts deferred** (§1): the inference-template registry leaves the core; inferred
  derivations → human `add_fact`/review.
- **`add_fact` real-note rule** (v1) stands; the committer re-derives domain from the cited note.

---

## 6. Security deltas (vs v1 §6)

- **`domain_move` = publish, tombstone-not-purge** (§4): removes the destructive purge;
  derivation-before-undo accepted as inherent to publishing (bounded). One-way preserved.
- **Snapshot-undo re-derives domain on un-tombstone** (NEW-4): a frozen `domain_code` can never
  bypass the committer's live firewall re-derivation.
- **Global attribute-free canonical index — residual 1-bit channel ACCEPTED-RISK** (NEW-2):
  documented, not silent. For a **single-user** system the owner already may see all their own
  domains via the authorized resolver; the residual is a constant-work footprint/timing channel
  worth ≤1 bit/query, mitigated by the constant-work gate and owner-only access. Re-graded
  SEV-2, accepted with the mitigation; flagged in §10 as the one asset to watch.
- **Resolver "decoy/constant-work" right-sized** (ergonomics N7): keep constant-work (timing-
  blind) but drop gold-plated decoy padding beyond what the single-user threat model needs.
- The v1 §6 isolation-test list stands, **plus**: un-tombstone-domain-re-derivation test;
  publish-is-tombstone-not-destroy test; set-predicate contradiction-review test; co-location
  typing test; absent-range→review test.

---

## 7. Temporal deltas — unchanged from v1 §7 (no-auto-abutment; rrule edits preserve
exceptions; expand-from-now + cached `next_occurrence_at`; explicit realization). The
former/superseded two-axis reconciliation is clarified in §3 (R2-5).

---

## 8. Invariant check (amends v1 §8)

- **Audit & reversibility — AMENDED, honestly.** Framing §4 "every change is unwindable" is
  satisfied in the **bounded** sense: snapshot-revert with cascade-or-block; a non-tail undo
  that would strand a later dependent op is offered as "**correct instead**" (a forward op),
  not silently performed. This is an explicit, documented amendment — the spec **no longer ticks
  §4 as unconditionally met** (migration R2-1). It is defensible because R1 proved no sound
  inverse-rebase exists; this is the honest maximum.
- **RLS firewalls — strengthened**, with the global canonical index's residual 1-bit channel as
  a documented bounded ACCEPTED-RISK (§6/§10).
- All other invariants (LLM-adapter, storage abstraction, bitemporal, #7) — ✔ as v1, with the
  simplifications (no `fact_current` table, 5 variants) reducing surface.

---

## 9. Simplification scorecard (v1 → v2 net)

**Deleted:** the separate materialized `fact_current` table + its per-op dual-write; 2
TypedValue variants (7→5); the inferred-fact auto-commit path + inference-template registry from
the core; the destructive "purge on undo." **Tightened (not enlarged):** value-identity
uniqueness, co-location typing, set-contradiction check, un-tombstone re-derivation. **Net:**
v2 is smaller and safer than v1; the only true addition is the `realize` op (which replaces an
under-specified reference).

---

## 10. Decisions still genuinely open (for the user, at final sign-off — not blocking)

1. **Cross-domain identity resolver + global canonical index (carried from v1).** Per-domain
   projections + attribute-free index is the pick; the residual 1-bit existence/co-membership
   channel (security NEW-2) is an accepted bounded risk for a single-user system. *Confirm* you
   accept it, or prefer the simpler global-entity-table + attribute-RLS alternative.
2. **`add_fact` strictness (carried).** Direct `add_fact` only when it cites a real note span;
   else forced correction-note round-trip; non-droppable attribution. Confirm strictness.
3. **Undo promise (NEW, needs explicit blessing).** The reversibility invariant is satisfied in
   the **bounded** sense above (snapshot + cascade-or-block; non-tail removal = forward
   correction). This is weaker than "anything is unwindable in any order." *Confirm this framing
   is acceptable* (it is the honest maximum; v1 over-promised).
4. **Inferred facts deferred.** Auto-derived facts (age→birth_year) are **not** in v1/v2 scope;
   they route to human add_fact. Confirm deferral, or prioritize the template feature.
5. **TypedValue at 5 vs 7.** v2 ships 5 (`boolean`→`enum`, `structured` on demand). Confirm, or
   keep `structured`/`boolean` first-class from day one.

---

## 11. Round-2 disposition table

**Correctness:** R2-1 member fork/natural-key collision → FIXED (per-(slot,natural_key) UNIQUE +
idempotent-merge, §3). R2-4 per-interval history → FIXED (per-member chains, §3). R2-2 set
contradiction never fires → FIXED (modality-stripped contradiction index, §4). R2-3 realize op
missing → FIXED (defined, §4). R2-5 no-abutment vs functional live-index → FIXED (two-axis
former=valid-time + tx-supersession, §3). R2-6 merge_facts provenance/modality → FIXED (union
provenance, reject cross-modality, §4). R2-7 undo slot-occupancy → FIXED (re-check + route to
review, §4).

**Model:** SEV-1.1 typing oracle (which in-range / absent range) → FIXED (mandatory range
coverage + subject co-location, §5). SEV-1.2 inferred premise auto-commit → FIXED-BY-DEFERRAL
(inferred no longer auto-commits, §1/§5). SEV-2.1 general-domain irrealis → FIXED (modality never
model-trusted, §2). SEV-2.2 field-omission → FIXED (structural-completeness guard, §5).

**Security:** NEW-1 move laundered via derivation-before-undo → ACCEPTED-RISK (publish is
inherently irreversible; bounded owner-only/audited/told, §4/§6). NEW-2 global index 1-bit oracle
→ ACCEPTED-RISK (documented, single-user bound, §6/§10). NEW-3 one-way purge destroy/desync →
FIXED (tombstone-not-purge, §4). NEW-4 snapshot-undo frozen domain → FIXED (re-derive domain on
un-tombstone, §4).

**Migration:** R2-1 undo invariant honesty → FIXED (explicit bounded §4 amendment, §8). R2-2
snapshot metadata re-derive → FIXED (re-derive protection metadata on un-tombstone, §4).

**Ergonomics:** N1 materialized fact_current → FIXED (deleted; partial-index lookup, §1/§3). N2
three keys threaded → FIXED (single `keys()` fn, §3). N4 inference registry over-built →
FIXED-BY-DEFERRAL (§1). N5 cascade-undo comprehensibility → FIXED (undo-last/revert-to-point/
correct-instead surface, §4). N7 decoy resolver gold-plating → FIXED (right-sized, §6). F7 7-vs-5
variants → FIXED (ship 5, §2). N3/N6/N8 → SEV-3 noted.

**Performance:** R2-N1 fact_current write amplification → FIXED (table deleted, §1/§3). R2-N2
key on mutable canonical → FIXED (stable `predicate_id`, §3). R2-N3 overlay diff unbounded →
DEFERRED (future-re-analysis-only; span-GiST/blast-radius index specced then). R2-N4 constant-
work resolver tax → ACCEPTED (bounded small constant, dwarfed by LLM calls; not a cliff).
R2-N5/N6 → SEV-3 (checkpoint cadence/retention; bounded batch dependency walk).

---

**Round-2 outcome:** all Sev-1 FIXED or FIXED-BY-DEFERRAL; all Sev-2 FIXED or explicitly
ACCEPTED-RISK with a stated bound. Remaining items are SEV-3 nits and the five §10 sign-off
decisions. v2 goes to a focused Round 3 (correctness, model, security re-verify) to confirm
convergence before the final spec.
