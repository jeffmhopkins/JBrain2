# Red-team R3 — Correctness & edge cases (fact-pipeline-redesign)

**Lens:** correctness & edge cases. **Target:** `22-spec-v2.md` (deltas over v1 after R2).
**Inputs:** `00-framing.md` (wishlist §2, invariants §4), `01-decisions-log.md`,
`31-redteam-r2-correctness.md` (R2 findings R2-1…R2-11).
**Posture:** two jobs. (1) VERIFY each R2 finding is GENUINELY closed by v2's *mechanism*.
(2) Attack v2's NEW/changed mechanisms — the per-(slot,natural_key) UNIQUE + idempotent-merge
value-identity rule, per-member history chains, the `realize` op, the modality-stripped
contradiction index, the two-axis former, undo→review-on-occupancy — for fresh bugs.

Severity key: **SEV-1** breaks an invariant or core goal · **SEV-2** serious, must-fix
before sign-off · **SEV-3** nit / documentable accepted-risk.

---

## PART 1 — Verification of R2 findings

| R2 | v2 mechanism (§) | Verdict |
|---|---|---|
| **R2-1** member fork / NK-map collision | per-`(slot,natural_key)` UNIQUE + deterministic idempotent-merge; minted id only when no NK; healed on entity-merge (§3) | **PARTIALLY CLOSED** — the *fork* (mint-race) and *NK-map collision* legs are genuinely closed by the UNIQUE constraint. But the constraint **introduces the inverse bug**: distinct people sharing a normalized natural key are now force-MERGED. See R3-1 (SEV-1). |
| **R2-4** per-interval history | per-`value_identity` supersession chains; `history(slot)` per-member, never union-by-`identity_key` (§3) | **NOT GENUINELY CLOSED** — for value-EXCLUDING functional `live_key`, the Acme/Globex/Acme case has the two Acme stints sharing **one** `value_identity`, so "per-member chain" is still one chain that cannot represent two disjoint intervals. See R3-2 (SEV-1). |
| **R2-2** set contradiction never fires | modality-stripped contradiction index on `(subject,predicate_id,value_identity)` → review (§4) | **CONFIRMED-CLOSED** in mechanism (a dedicated index that ignores modality now exists). One residual: the comparison key omits `qualifier`/`domain` — see R3-5 (SEV-2). |
| **R2-3** realize op missing | `realize(fact,to_modality='asserted')` supersedes irrealis row on **same `value_identity`** (§4); only path to live floor; no wall-clock auto-promote | **CONFIRMED-CLOSED** in existence — but the re-key interaction with the live-index/contradiction path is under-specified. See R3-3 (SEV-2). |
| **R2-5** former vs functional live-index | two axes: former = `valid_to.bound=unknown` (valid-time) **and** tx-supersession (`tx_to` set, `state='superseded'`); live index keys on `live_key`+`tx_to IS NULL` (§3) | **CONFIRMED-CLOSED.** The two axes are now explicitly separated and the superseded row is out of the live set; clean. |
| **R2-6** merge_facts laundering | provenance unioned never dropped; cross-modality merge rejected; cross-domain rejected; conflicting values → review (§4) | **CONFIRMED-CLOSED** for modality-laundering + provenance-drop. Residual: "provenance unioned" still needs a multi-provenance carrier; schema wiring unstated (rolls to R3-6, SEV-3). |
| **R2-7** undo slot-occupancy | on un-tombstone, (i) re-check `live_key` occupancy → if occupied, surface as forward-correction conflict to review (never a throwing insert); (ii) re-derive metadata+domain; (iii) note-contradicting undo→review (§4) | **CONFIRMED-CLOSED.** The occupancy re-check + route-to-review closes the throw/two-live break exactly as the R2 fix demanded. |

**Net:** R2-2, R2-3 (existence), R2-5, R2-6, R2-7 **confirmed closed**. **R2-1 and R2-4 are
NOT genuinely closed** — R2-1's fix over-corrected into a force-merge of distinct entities
(R3-1); R2-4's per-member-chain claim does not actually segment the resumption case (R3-2).
Both are NEW SEV-1s arising directly from the R2 "fixes."

---

## PART 2 — New SEV-1 findings

### R3-1 · Normalized-name natural key force-MERGES distinct people: the per-`(slot,natural_key)` UNIQUE + idempotent-merge collapses two different "John Smith"s into one member

**Severity: SEV-1.**

**Where:** §3 value-identity rule — priority `object_id` → **declared natural key (E.164
phone, lowercased email, normalized name)** → minted id only when no NK; "a mint-race on the
same natural key resolves by deterministic idempotent merge on that key (the UNIQUE constraint
makes the second insert a no-op/merge, never a fork)."

**Scenario.** Set predicate `person.knows` (or `person.child`, `org.employee`). Note 1: "Sam
knows John Smith (the lawyer)." Stage-2 retrieval misses → no `object_id`; natural key falls to
**normalized name `john smith`**. Member written with `value_identity = nk(john smith)`. Note 2,
unrelated context: "Sam knows John Smith (the plumber)." Retrieval misses again → same
normalized natural key `john smith`. The per-`(slot,natural_key)` UNIQUE constraint now makes
the second insert a **no-op/idempotent-merge** — the two **distinct** John Smiths collapse into
**one** set member. R2 asked to prevent a *fork* (one person appearing twice); v2's fix instead
guarantees a **false merge** (two people appearing as one) whenever the natural key is a
human-name string, which is the *most common* set-member case in the wishlist (§2.9 children,
contacts). Name is not a unique identifier; treating it as one under a UNIQUE constraint is a
correctness inversion. The same applies to two people legitimately sharing a phone number
(household landline) or a shared/role email (`info@…`).

**Why SEV-1.** The framing's single most-emphasized requirement is override-vs-array member
correctness (§2.9). v1 had a fork bug; v2 trades it for a *silent identity collision* that is
**worse** (data loss / cross-person attribution — and for `health`/`finance` set predicates, a
firewall-adjacent mis-attribution). It fires with **zero human error**, on the commonest natural
key. The R2-disposition table marks R2-1 "FIXED"; it is not — the failure mode merely flipped
sign.

**Fix.** Natural-key identity must be **gated by key quality**: only *globally-unique*
identifiers (E.164 phone, lowercased email, `object_id`) may key the UNIQUE merge. A
**normalized name is NOT a merge key** — it is a *match hint* that routes an ambiguous
second-mention to **review/disambiguation** (or mints a distinct provisional member), never a
silent merge. State per natural-key-type whether it is *authoritative* (unique → merge) or
*advisory* (name → review). Add a golden test: two distinct same-name members stay distinct
(routed to review), two same-phone household members route to review, never auto-merge.

---

### R3-2 · Per-member supersession chains still cannot represent a RESUMPTION: Acme→Globex→Acme collapses to one Acme member with a contradictory chain

**Severity: SEV-1.**

**Where:** §3 R2-4 fix: "history is reconstructed per `value_identity`'s own supersession chain
— each member is its own interval series … never a naive union-by-`identity_key`." §3 keys:
functional `live_key` = identity_key **minus value_identity**.

**Scenario.** Functional-over-time `person.employer`. Stints: Acme [2019,2023), Globex
[2023,2025), Acme-again [2025,open). `value_identity` for both Acme stints is identical (same
employer object/natural key). v2 says reconstruct "per `value_identity`'s own supersession
chain." But the two Acme intervals share **one** `value_identity` → they are the **same member
key** → there is exactly **one** Acme chain node, not two. So "each member is its own interval
series" gives: member=Acme, member=Globex — **two members, not three intervals**. The
resumption [2025,open) either (a) overwrites/supersedes the [2019,2023) Acme row on the same
`value_identity` slot (losing the first interval entirely), or (b) is stored as a second row
with the same `value_identity` — at which point the per-`value_identity` chain has a **branch**
(two Acme rows, one chain key) and the spec gives no rule to keep them as two disjoint intervals
rather than one. The R2 fix-text explicitly promised "never a naive union-by-identity_key that
would collapse `[2019–2023]+[2025–open]` across a gap" — but `value_identity` *is* the per-value
key, so grouping by it **is** that very union. The fix renamed the grouping key but did not add
the gap-segmentation rule R2-4 actually demanded.

**Why SEV-1.** v2's §11 marks R2-4 "FIXED (per-member chains, §3)." It is not: per-member
chains and per-value grouping are the **same key** for a functional predicate, so the
resumption case — the exact SEV-1 — is still unrepresentable. The history derivation that R2-4
asked to be *specified* (segment into maximal contiguous valid-time runs, do not union across a
gap) is **still absent**; only the name changed.

**Fix.** The interval identity for a functional-over-time history must be **(value_identity,
valid-time run)**, NOT value_identity alone. Specify: group rows by `value_identity`, then
**segment each group into maximal contiguous valid-time runs**, emitting one stint per run; a
resumption after a gap is a NEW interval under the same `value_identity`, never merged with the
prior. Each interval is its own supersession-chain node keyed `(value_identity, valid_from)`.
Add the golden test R2-4 already specified: Acme/Globex/Acme returns exactly three intervals,
two of them Acme, with no [2019,open) collapse.

---

## PART 3 — New SEV-2 findings

### R3-3 · `realize`'s "same `value_identity`" re-key is under-specified against the live-index and contradiction path — a realize can throw or create two-live

**Severity: SEV-2.**

**Where:** §4 `realize(fact, to_modality='asserted')` "supersedes the hypothetical/expected row
with an asserted row on the **same `value_identity`** (a modality re-key)." §3 keys: `modality`
is IN `identity_key`; `live_key` drops "modality-non-asserted from the live floor."

**Scenario.** `person.employer` (functional), candidate row `hypothetical: Globex` live in the
candidate floor; a separate `asserted: Acme` is the current live value. Owner runs
`realize(Globex)`. The realize "supersedes the hypothetical row with an asserted row on the same
value_identity" — but for a *functional* predicate the realized asserted Globex must take the
**functional `live_key`** (value-excluding), which Acme already occupies. v2 §4 R2-7 fixed
*undo* against occupancy, but `realize` is a *forward* op and the spec does **not** say it runs
the same occupancy check. Two outcomes, neither specified: (a) the asserted-Globex insert hits
`one_live_per_live_key` and **throws**; or (b) it silently supersedes Acme — i.e. `realize`
becomes a hidden `supersede`, promoting a *hypothesis* over an *asserted* current value with no
contradiction review. The R2-3 fix added the op but did not wire it to the live-selection /
contradiction machinery that the modality-in-key model requires on every floor-bound write.

**Why SEV-2.** `realize` is now "the *only* path from irrealis to the live floor" (§4), so every
hypothesis→fact transition flows through it; its undefined interaction with an already-live
asserted value on a functional `live_key` is on the hot path. A hypothesis silently overwriting
an asserted current value (outcome b) re-opens the S1-1/S1-2 modality-safety SEV-1 through the
realize door.

**Fix.** Specify that `realize` runs the **same live-floor reconciliation as any asserted
write**: (i) for a functional predicate, if a *different* asserted value occupies the target
`live_key`, route to **contradiction/supersession review** (do not silently supersede, do not
throw); (ii) for a set predicate, the realized member joins the set normally; (iii) run the
modality-stripped contradiction check (R3-5) against any live `negated` twin. Add a test:
realize of a hypothetical functional value while a different asserted value is live → review,
exactly one live row throughout.

---

### R3-4 · `realize`'s modality re-key changes `identity_key` (modality is in it) — the supersession link and the candidate's own history are broken

**Severity: SEV-2.**

**Where:** §3 `identity_key` = `(predicate_id, qualifier, subject, domain, modality,
value_identity)` — **modality is a component**. §4 `realize` "supersedes … on the same
value_identity (a modality re-key)."

**Scenario.** The candidate row is `identity_key = hash(…, modality=hypothetical, vi=Globex)`.
`realize` writes an asserted row: `identity_key = hash(…, modality=asserted, vi=Globex)` — a
**different identity_key** (modality flipped). v2 says they share the same *value_identity*, and
the asserted row `supersedes` the candidate. But supersession and the two-key model are defined
*within* an `identity_key` lineage (an identity's own append-only chain); a `supersedes` edge
that **crosses identity_key boundaries** (hypothetical-key → asserted-key) is a new construct the
spec doesn't characterize. Consequences: (a) `history(slot)` reconstruction that walks chains
*within* an identity_key (R2-4/R3-2) won't traverse the hypothetical→asserted hop, so the
pre-realization hypothesis vanishes from the realized fact's history; (b) an **undo of
`realize`** must un-supersede across the identity_key boundary — but the un-tombstone occupancy
re-check (§4 R2-7) keys on `live_key`, and the hypothetical candidate's `live_key` (modality-
non-asserted, off the live floor) is *not* the asserted `live_key`, so the cross-key restore is
again unspecified.

**Why SEV-2.** "Same value_identity" is not the same as "same identity_key"; v2 leans on the
former to claim a clean re-key while the latter (which actually governs chains, history walks,
and undo) silently changes. The op is defined but its lineage semantics across the modality
component of `identity_key` are not — leaving history-loss and an undefined undo.

**Fix.** State explicitly that `realize` records a **cross-modality supersession edge** that
`history(slot)` and undo both traverse: the realized asserted row's chain includes its
pre-realization hypothetical antecedent (so history shows "expected Globex → realized
[date]"), and `realize`'s undo restores the hypothetical row AND clears the asserted row's
`live_key` occupancy. Make the `keys()` function emit a stable `lineage_id` (independent of the
modality component) that supersession/history/undo key on, so a modality re-key stays within one
lineage.

---

### R3-5 · Modality-stripped contradiction index drops `qualifier`/`domain` — false-positive contradictions and a cross-firewall comparison

**Severity: SEV-2.**

**Where:** §4 R2-2 fix: contradiction check compares an `asserted`+`negated` pair on
`(subject, predicate_id, value_identity)` — **modality-stripped**, but also `qualifier`- and
`domain`-stripped.

**Scenario A (false positive).** `person.nickname` qualified by audience: asserted
`nickname{audience=work}="Sam"` and a `negated nickname{audience=family}="Sam"` ("family does
NOT call him Sam") are *not* contradictory — different qualifier. But the contradiction key
`(subject, predicate_id, value_identity)` omits `qualifier`, so it flags them as an
asserted/negated contradiction and raises a spurious review item. Over-firing trains the owner
to dismiss contradiction reviews — eroding the very health-safety signal R2-2 was added for.

**Scenario B (cross-domain compare).** The contradiction index keys on `(subject,
predicate_id, value_identity)` with no `domain` component, so it compares an `asserted` row in
`health` against a `negated` row in `general` for the same subject/predicate/value. Beyond
false positives, the comparison itself **reads across the domain firewall** to detect the pair —
a review item that exists only because a health row and a general row were joined. Even if the
review is benign, the *detector query* must touch both bands, which the §6 isolation tests
must now explicitly permit-or-forbid; the spec leaves the detector's RLS scope unstated.

**Why SEV-2.** R2-2's fix is correct in *stripping modality* but over-strips: it also strips
`qualifier` (semantic) and `domain` (firewall). The first over-fires reviews; the second
introduces an unspecified cross-band read. Both undercut the safety goal and the RLS invariant.

**Fix.** The contradiction key strips **only** modality:
`(owner, subject, predicate_id, qualifier, domain, value_component)`. The detector runs
**per-domain (RLS-scoped)** — a contradiction is only meaningful within one band; never join
across firewalls. Add §6 isolation test: the contradiction detector raises no item from a
cross-domain pair and issues no cross-band read.

---

### R3-6 · `merge_facts` "provenance unioned" has no multi-provenance carrier defined; idempotent-merge (R3-1) inherits the same gap

**Severity: SEV-2.**

**Where:** §4 R2-6 fix: "provenance is unioned, never dropped." §3 idempotent-merge on a
natural key (the merge of a mint-race second insert).

**Scenario.** `merge_facts(A,B)` where A cites note n1/span and B cites note n2/span (same
domain, so cross-domain reject doesn't fire). v2 mandates "provenance unioned, never dropped" —
but v1's provenance model is a **single `note_id`/`span` per row** (carried into v2; §2 is
unchanged on this), and v2 specifies **no** multi-provenance carrier (no corroboration-child
wiring). So "unioned" has nowhere to land: the merged row can hold one provenance, forcing
either a silent drop (violating the fix) or an undefined schema. The **idempotent-merge on a
natural key** (§3, R3-1's mechanism) has the *same* gap: when the second same-natural-key insert
no-ops/merges, the second note's attribution must be unioned onto the surviving row — same
missing carrier.

**Why SEV-2.** Two v2 mechanisms ("merge_facts unions provenance" and the value-identity
idempotent-merge) both *depend on* a multi-provenance representation the spec never defines.
Without it, every merge is an audit-gap (a note's attribution silently lost) — violating the §4
audit invariant.

**Fix.** Define the multi-provenance carrier once (a `fact_provenance` child table: many
`(note_id, span, reported_at)` per fact, RLS-scoped to the fact's domain) and route **both**
`merge_facts` and the idempotent natural-key merge through it. Add an audit test: a merge of two
differently-sourced facts yields a row whose provenance set = the union of both inputs.

---

## PART 4 — SEV-3 / nits

- **R3-7 · `realize` reverse direction undefined.** §4 defines `realize` only toward
  `asserted`; "this turned out to be hypothetical after all" (demote an asserted row to
  hypothetical) has no op. Likely a forward `set_field`-style correction, but its interaction
  with the live-index (vacating a functional `live_key`) is unstated. Documentable; mirrors R3-3.
- **R3-8 · Idempotent-merge determinism under concurrent value churn.** §3 "deterministic
  idempotent merge on that key" — for `ref`/`object_id` keys this is fine, but when two
  concurrent ops merge onto the same natural key with *different* non-key fields (confidence,
  reported_at), "deterministic" needs a stated tie-break (last-tx-wins / max-confidence).
  Documentable; rolls into R3-6's carrier (union the fields too).
- **R3-9 · `valid_to.bound=unknown` former + a later contradicting note.** Two-axis former
  (R2-5) leaves the superseded row with `bound=unknown`. If a later note supplies the real end
  date for that already-superseded value, no op is specified to backfill `valid_to` on a
  superseded (tx-closed) row for history rendering. Documentable; allow a history-only
  valid-time amend on superseded rows.
- **R3-10 · 5-variant `TypedValue`: `enum` absorbing `boolean` + `ref` plausibility.** R2-11's
  structured-sub-field plausibility gap (B2b not recursive) is unaddressed by v2 (structured
  deferred), and now `enum` carries booleans — confirm the co-location/range guard (§5) applies
  to `enum` membership, not just quantity/date. Documentable.

---

## Summary

**R2 verification:** R2-2, R2-3 (op exists), R2-5, R2-6, R2-7 — **CONFIRMED-CLOSED** (5).
**STILL-OPEN / not genuinely closed:** **R2-1** (fix over-corrected into a force-merge of
distinct entities — R3-1) and **R2-4** (per-member chain == per-value key for functional, so
the resumption interval is still unrepresentable — R3-2). Both spawned NEW SEV-1s.

**New findings:** SEV-1 ×2 (R3-1 normalized-name force-merge; R3-2 resumption interval
collapse). SEV-2 ×4 (R3-3 realize vs live-index/contradiction; R3-4 realize modality re-key
breaks lineage/history/undo; R3-5 contradiction index over-strips qualifier+domain; R3-6
provenance-union has no carrier). SEV-3 ×4.

**Recurring root causes (R3):** (1) the R2-1/R2-4 fixes conflated *identity* with *uniqueness*
and *grouping* with *interval-segmentation* — a UNIQUE constraint on a non-unique key
(name) force-merges (R3-1), and grouping by the per-value key cannot segment a gap (R3-2).
(2) The new `realize` op was added to the algebra but not wired to the live-floor / contradiction
/ lineage / undo machinery the modality-in-`identity_key` model demands on every asserted write
(R3-3, R3-4). (3) "Strip modality" over-stripped to also drop qualifier+domain (R3-5), and
"union provenance" still has no carrier (R3-6). **R2-1 and R2-4 must NOT be marked FIXED at
sign-off; the `realize` op needs a full live-floor/lineage spec before it is.**

*End R3 (correctness lens). New SEV-1 ×2, SEV-2 ×4, SEV-3 ×4. Not converged.*
