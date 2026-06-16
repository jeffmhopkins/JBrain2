# Red-team R2 — Correctness & edge cases (fact-pipeline-redesign)

**Lens:** correctness & edge cases. **Target:** `21-spec-v1.md` (integrated spec v1),
the revision that claims to fold every R1 Sev-1/Sev-2. **Inputs:** `00-framing.md`
(wishlist §2, invariants §4), `30-redteam-r1-correctness.md` (my R1 findings S1-1…S2-8,
S3-*).
**Posture:** adversarial, two jobs. (1) VERIFY each R1 finding is genuinely closed by v1's
*mechanism*, not just its disposition-table prose. (2) Attack v1's NEW mechanisms — the
two-key model, snapshot/state undo under non-LIFO interleavings, the realization op,
`merge_facts`, no-auto-abutment, the 3-way overlay diff — for fresh correctness bugs.

Severity key: **SEV-1** breaks an invariant or a core goal · **SEV-2** serious, must-fix
before sign-off · **SEV-3** nit / documentable accepted-risk.

---

## PART 1 — Verification of R1 findings

| R1 | v1 mechanism | Verdict |
|---|---|---|
| **S1-1** negation re-asserts via shared key | `modality` in BOTH keys (§3.2); `current()` asserted-only (§3.3); asserted-vs-negated = contradiction review | **CONFIRMED-FIXED** — but see R2-2 (the contradiction-review *trigger* is under-specified for set predicates). |
| **S1-2** `current()` ignores modality | modality is 3rd live gate; candidate floor; explicit promotion op (§3.3) | **CONFIRMED-FIXED** in mechanism — but see R2-3 (the promotion op itself is never defined in the §4.1 algebra). |
| **S1-3** member drift / resurrection / fork | tombstone-vs-readd→review; `merge_entities` re-key pass; minted member-id (§3.2) | **PARTIALLY OPEN** — resurrection & merge legs fixed; the *fork* leg has a residual ordering hole, see R2-1. |
| **S1-4** functional-over-time two readings | two identities: value-including `identity_key` + value-excluding `live_key` (§3.2/§3.3) | **STILL-OPEN (mechanism gap)** — the two keys do not by themselves reconstruct per-interval history; see R2-4. |
| **S2-1** split→re-extract not idempotent | 3-way diff; `split_lineage`; span-overlap→review (§5.3) | **CONFIRMED-FIXED.** |
| **S2-2** `merge_facts` undefined temporal/modality/domain | cross-domain reject; explicit args; non-trivial Allen reject (§4.1) | **CONFIRMED-FIXED** for the named axes — but see R2-6 (provenance-domain union still leaks; merge of contradictory modality under-defined). |
| **S2-3** recurrence exception breaks on rule edit | rrule/dtstart edits reconcile every exdate/rdate/override (§2.6) | **CONFIRMED-FIXED.** |
| **S2-4** `replace_head`+valid-time → dup/two-live | `replace_member` mints value-decoupled stable `value_identity` + natural-key map (§3.2) | **CONFIRMED-FIXED** for the named case — but see R2-1 (the natural-key→member map has no collision/merge rule). |
| **S2-5** locale/within-variant misparse | B2b range + within-variant low-conf review + locale context (§2.2) | **CONFIRMED-FIXED.** |
| **S2-6** auto-abutment fabricates end date | auto-abutment dropped; former-without-date (`bound=unknown`/`certainty=inferred`) (§2.6) | **CONFIRMED-FIXED** — but creates a NEW current-value bug, see R2-5. |
| **S2-7** bitemporal undo leaves two live/gap | snapshot-undo re-validated; dependency graph blocks/cascades (§1.2) | **PARTIALLY OPEN** — block/cascade defined, but un-tombstone re-validation against the live-key unique index is unspecified; see R2-7. |
| **S2-8** scheduled/expected auto-flip | no auto-promotion on `now` crossing `valid_from`; explicit realization op (§3.3) | **CONFIRMED-FIXED** in principle — same missing-op gap as R2-3. |

**Net:** 8 confirmed-fixed, **S1-3 / S1-4 / S2-7 not genuinely closed** (residual mechanism
gaps surfaced below as R2-1, R2-4, R2-7), and two "fixed" items spawned NEW bugs
(R2-5 from S2-6's fix; R2-3 from the promotion-op that S1-2/S2-8 assume but never define).

---

## PART 2 — New SEV-1 findings

### R2-1 · Set-member fork still occurs: `value_identity` priority is order-dependent and the natural-key map has no collision rule (S1-3 fork leg NOT closed)

**Severity: SEV-1.**

**Where:** §3.2 `value_identity` priority (object-canonical-id → natural key → minted
member-id); the `replace_member` "natural-key map" (§3.2); `merge_entities` re-key pass.

**Scenario (fork survives the fix).** Set predicate `person.child`, member identity priority
1 = object canonical-id. Note 1 mentions "Lydian"; Stage-2 retrieval MISSES (cold index) →
provisional mint `ent_lydian_1` (§2.3 allows mint-on-miss). Member written with
`value_identity = canonical(ent_lydian_1)`. Note 2 (later, different context) mentions
"Lydian"; retrieval again misses → provisional mint `ent_lydian_2`. Now TWO live set members
with distinct `value_identity`. v1's fix says `merge_entities` runs a re-key/dedup pass — but
**`merge_entities` is a human/owner op (§4.1 group F); nothing auto-merges the two provisional
mints.** The deferred-dedup pass (§2.3) is described only for *mint dedup at the entity layer*,
and §3.2 only heals members *"whose `value_identity` object-ids now resolve to one canonical"*
— which requires the entity merge to have *already happened*. Until a human notices and merges,
the daughter appears **twice in current-value**. R1 S1-3 leg B is therefore reduced in
likelihood (retrieval improved) but **not eliminated**; the spec's own §2.3 "mint forced by a
retrieval miss is provisional" concedes misses still mint.

**Scenario (the new natural-key map is itself a fork engine).** §3.2 says `replace_member`
keeps a stable minted id and "a future re-extraction of the new number matches via the
member's recorded natural-key map." But the map is **per-member and one-directional**. Member
M (minted id `vi_m`) has recorded natural key `+15550100` after a `replace_member`. Now a
*different* member N of the same set is independently `replace_member`'d to `+15550100` (data
entry error, or the same number legitimately reassigned). Two members now claim the same
natural key in their maps. A re-extraction of `+15550100` matches **both** maps — the spec
gives no tie-break, so it either double-matches (updates two members) or picks one
nondeterministically. Symmetrically, nothing forbids two minted members from ever colliding on
a natural key, so the "decoupled minted id" guarantee that S2-4 relies on silently breaks for
sets with value churn.

**Why SEV-1.** The framing's single most-emphasized requirement is override-vs-array member
correctness (§2.9, "member identity drift across reprocessing"). v1 closed the *re-spell* and
*replace* cases but left **mint-race fork** (no auto-merge of provisional mints) and **natural-
key-map collision** (no uniqueness/tie-break) — both produce duplicated or mis-routed members
with zero human error.

**Fix.** (a) The deferred-dedup pass (§2.3) must run at the **member layer too**: after it
merges two provisional canonicals it MUST trigger the §3.2 re-key/dedup pass automatically (not
only on a human `merge_entities`). (b) The natural-key→member map must be **unique per
(slot_key, natural_key)**; a second member adopting a natural key already mapped raises a
member-collision review, never a silent double-map. (c) A re-extraction matching >1 member map
routes to review.

---

### R2-4 · The two keys group history but do NOT reconstruct per-interval value history — S1-4's core complaint is unaddressed

**Severity: SEV-1.**

**Where:** §3.2/§3.3/§7(i). The "two identities" fix; `identity_key` includes value,
`live_key` excludes it for functional.

**Scenario.** `person.employer` is functional-over-time. Stints: Acme [2019,2023), Globex
[2023,2025), Acme-again [2025,open). v1 stores these **one-edge-per-value** (§3.3:
"functional-over-time is set-storage one-edge-per-value"). So the live_key (value-excluding) is
the same for all three — good, it enforces one-current. The `identity_key` (value-including)
groups *by value*: both Acme rows hash to one `identity_key`, Globex to another. **This is
exactly what S1-4 asked for as the grouping.** BUT: the two Acme stints share **one
`identity_key`** and are **distinguished only by valid-time + the `supersedes` chain**. The
spec never states how "Acme: 2019–2023 AND 2025–present" is reconstructed as *two distinct
intervals* rather than one. Worse: under the **value-EXCLUDING `live_key`**, the supersession
chain is Acme→Globex→Acme — so the second Acme row's `supersedes` points at Globex, **not at
the first Acme row**. The `identity_key` says "these two Acme rows are the same identity"; the
`supersedes` chain says "the second Acme came after Globex." There is **no edge linking the two
Acme intervals as a resumption vs. a continuation**, and the spec specifies neither a re-
segmentation algorithm nor an interval-merge rule. A naive "group by identity_key, take
valid-time union" would collapse [2019,2023) ∪ [2025,open) into one bogus [2019,open) interval
spanning the Globex gap.

**Why SEV-1.** v1's §9 marks S1-4 "FIXED … now resolved." It is not: the two keys solve
*grouping* and *one-current* but the **per-interval history reconstruction** that was the actual
SEV-1 ("reconstructing Acme 2019–2023 and 2025–present requires re-segmenting by value which the
spec never specifies") is still unspecified. Storing one-edge-per-value made it *recoverable in
principle* but the spec ships no derivation, and the obvious one (union by identity_key) is
wrong across gaps.

**Fix.** Specify the interval-history derivation explicitly: group by `identity_key`, then
segment into **maximal contiguous valid-time runs** (do NOT union across a gap occupied by a
different live_key value), each run = one "stint." State that the `supersedes` chain is the
*live_key* succession (one-current) and is intentionally orthogonal to the per-value interval
grouping; a resumption is a NEW interval under the same identity_key, not a continuation of the
prior one. Add a golden test: Acme/Globex/Acme returns exactly three intervals, two of them
Acme.

---

## PART 3 — New SEV-2 findings

### R2-2 · Asserted-vs-negated contradiction review never fires for SET predicates — the two share neither live_key nor a comparison path

**Severity: SEV-2.**

**Where:** §3.2 ("if both are live a contradiction review fires"); §1.5; §3.3.

**Scenario.** `health.allergy` is `set` (the §7j default). "Sam is allergic to penicillin"
(asserted) and "Sam is NOT allergic to penicillin" (negated) are committed. v1 puts `modality`
in both keys, so they are **distinct live_keys** (different modality component) AND **distinct
value_identities** are not required — but for a *set* predicate the value_identity is derived
from the value `"penicillin"`, identical for both, while modality differs in the key. So they
are two live rows on **different live_keys**. The `one_live_per_live_key` unique index does NOT
fire (different keys). The contradiction review is described as triggering "if both are live" —
but **what query detects "both live with same (subject,predicate,value) but opposite
modality"?** The unique index can't (different keys by construction). No other mechanism is
specified. So the asserted and negated allergy coexist silently; `current()` returns the
asserted one (modality gate), the negated one sits in the candidate floor, and **no
contradiction review is ever raised**. The R1 S1-1 fix prevented the *silent overwrite*, but the
*positive* half of the fix ("an asserted-vs-negated collision is a contradiction review item")
has no firing mechanism for sets.

**Why SEV-2.** Health-domain safety: an explicit "NOT allergic to penicillin" correction note
and an asserted allergy now coexist with no reconciliation surfaced to the human — the user
believes their negation was recorded as a contradiction to resolve, but it silently floors.

**Fix.** Define the contradiction detector as a committer post-write check: on writing an
asserted member, query the candidate floor for a live `negated` row with the **same
`identity_key` modulo modality** (i.e. same subject/predicate/qualifier/domain/value), and vice
versa; if found, raise a contradiction-review item. This needs a *modality-stripped* index
(`hash(owner,subject,predicate,qualifier,domain,value_component)`) — neither existing key
provides it.

---

### R2-3 · The realization/promotion op that S1-2 and S2-8 depend on is not in the §4.1 algebra — the fix is referenced but unbuilt

**Severity: SEV-2.**

**Where:** §3.3 / §1.5 ("promotion … is an explicit op"); §4.1 op table.

**Scenario.** Spec body repeatedly asserts the hypothetical→asserted and expected→asserted
"realization" is "an explicit promotion op, never implicit." But the §4.1 ~12-op algebra lists
no such op. The closest is `set_field{...modality}`. Yet `set_field{modality}` flipping
`hypothetical`→`asserted` is **dangerous if it reuses the same identity_key/live_key**: per
§3.2, `modality` is IN both keys, so changing modality changes the keys — meaning a `set_field`
that mutates modality is NOT a same-slot field edit at all; it must **mint a new asserted row
under a different live_key while tombstoning the candidate row**, possibly *colliding* with an
already-live asserted row on that live_key (the `one_live_per_live_key` index then rejects the
write, or — if the field-edit path doesn't go through live-selection — leaves two live asserted
rows). The spec never says which. Because the dedicated promotion op doesn't exist, an
implementer will reach for `set_field{modality}`, and its interaction with the modality-in-key
invariant is undefined.

**Why SEV-2.** Two SEV-1 R1 fixes (S1-2, S2-8) are declared FIXED *by* "an explicit promotion
op," but the op is absent from the algebra and the only candidate (`set_field{modality}`)
violates the modality-in-key model. The fix is prose, not mechanism.

**Fix.** Add a first-class `realize` op (group A) that: validates the candidate row is
`hypothetical|expected`; checks no conflicting live asserted row on the target live_key
(else contradiction review); writes a new `asserted` assertion `supersedes`-linked to the
candidate; tombstones the candidate. Forbid `set_field{modality}` from crossing the
non-asserted↔asserted boundary (only intra-non-asserted modality edits allowed via set_field).

---

### R2-5 · No-auto-abutment (S2-6 fix) makes the OLD value linger as a second live interval — two concurrent "current" stints for a functional predicate

**Severity: SEV-2.**

**Where:** §2.6 ("supersession NO LONGER auto-abuts … prior marked former without a date,
`bound=unknown`"); §3.3 (`unknown` end ⇒ excluded from current-value); §3.1 live unique index.

**Scenario.** Functional-over-time `person.employer`. Acme [2019, open) live. Note: "now at
Globex" (no stated Acme end). Under v1's S2-6 fix, the committer must NOT close Acme to 2023;
instead Acme is marked **former without a date**: `valid_to.bound=unknown`,
`certainty=inferred`, status=former. Globex is the new value. Now examine live-selection.
Acme's row: is it `tx_to IS NULL`? The supersession should set Acme `state='superseded'`/
close `tx_to` — but the S2-6 fix is about **valid-time** (don't fabricate a valid_to), while
*supersession* is a **tx-time / state** operation. The spec conflates them: §2.6 says the prior
is "marked former" (a *valid-time* status) but does NOT say it is tx-superseded. If the prior is
left `state='live', tx_to IS NULL` (only valid_to.bound flipped to unknown), then under the
**functional live_key** (value-excluding, same for Acme and Globex) the `one_live_per_live_key`
unique index **rejects the Globex insert** (two live rows, same live_key) — the commit fails.
Conversely if Acme is tx-superseded, then "former without a date" is redundant with
supersession and the §3.3 rule "`unknown` end ⇒ excluded from current" never matters because
tx-supersession already excludes it. The spec hasn't reconciled the valid-time "former" marking
with the tx-time supersession + unique-index, so either the commit fails or the semantics are
double-specified and ambiguous.

**Why SEV-2.** The S2-6 fix (good in isolation) collides with the functional one-live-per-key
index (§3.1) and the supersession model. For functional-over-time the *whole point* is that the
prior leaves the live floor; "former without a date but still tx-live" cannot coexist with the
unique index.

**Fix.** Separate the two axes explicitly: supersession on a functional predicate **always
tx-supersedes** the prior (sets `state=superseded`, closes `tx_to`) so the live-index holds; the
"former without a date" is recorded as the prior's `valid_to.bound=unknown/certainty=inferred`
**on the now-superseded row** purely for history rendering. State that valid-time "former" and
tx-time "superseded" are independent and both applied. Add a test: superseding a functional
value with no stated end leaves exactly one live row AND the prior renders "former (no end
date)."

---

### R2-6 · `merge_facts` provenance-domain union and contradictory-modality merge still under-defined despite the S2-2 guards

**Severity: SEV-2.**

**Where:** §4.1 `merge_facts` ("cross-domain merges rejected; explicit temporal+modality
resolution; non-trivial Allen rejected").

**Scenario A (provenance domain).** Two facts in the SAME domain (so the cross-domain reject
doesn't fire), each citing a different note span. `merge_facts` "unions provenance spans"
(carried from C). But §2.5 provenance is a single `note_id`/`span`; the merged fact has ONE
provenance slot. Which note wins? If the merge keeps both as a corroboration child (§7l), fine —
but the spec doesn't wire `merge_facts` to the corroboration table; it's ambiguous whether merge
produces a multi-provenance row (schema has one) or silently drops one note's attribution
(audit gap).

**Scenario B (contradictory modality).** S2-2's fix requires "explicit modality resolution in
op args." But what if the operator picks `asserted` while merging an `asserted` and a `negated`
fact about the same value? The op would *manufacture* an asserted fact from a negated input,
laundering a negation into an assertion through merge — the very thing S1-1 forbids on the
supersession path, now reachable via `merge_facts` because merge takes modality as a free arg
with no guard that the inputs aren't modality-contradictory.

**Why SEV-2.** S2-2 closed temporal/domain-of-fact but left **provenance multiplicity** (one
slot, two notes) and **modality laundering via merge** open. The latter re-opens an R1 SEV-1
through a different op.

**Fix.** (a) `merge_facts` of facts with differing provenance must emit a multi-provenance row
via the §7l corroboration child (define the wiring), never drop a note. (b) Reject
`merge_facts` whose inputs have conflicting modality (asserted vs negated/hypothetical) — route
to contradiction review; the operator may not pick a modality that no input asserts.

---

### R2-7 · Snapshot-undo un-tombstones a superseded row without re-checking the live-key unique index — undo can still throw or resurrect a conflicting row

**Severity: SEV-2.**

**Where:** §1.2 ("undo = tombstone the assertions a target op wrote and un-tombstone the ones
it superseded"); §3.1 `one_live_per_live_key`; §4.5.

**Scenario.** v1 replaced precomputed inverses with snapshot-undo gated by an undo-dependency
graph (good — closes S2-7's "blind replay"). But consider: op `k` superseded Acme with Globex
(Acme→`state=superseded`, Globex→live). Later op `k+1` is a `realize`/`add` that writes a
DIFFERENT live asserted row sharing Acme's live_key (e.g. a `replace_member` correction, or a
move that re-used the value-excluding functional key). The dependency check (§1.2) asks "does a
later live op depend on k's outputs (same slot_key/value_identity/entity)?" — but `k+1` here
didn't *depend on* k's output (it didn't read Globex); it independently occupies the same
**live_key slot**. So the dependency graph sees no edge, undo of `k` proceeds, tries to
**un-tombstone Acme** → Acme goes live → now Acme AND `k+1`'s row are both live on the same
live_key → `one_live_per_live_key` **throws**, leaving the undo half-applied / aborted. The
"every change is unwindable" invariant fails for this interleaving — exactly the S2-7 failure
mode, now reachable because the dependency check keys on *data-dependency* (read-your-writes),
not on *live-key slot occupancy*.

**Why SEV-2.** v1's §9 marks S2-7 FIXED. The dependency graph closes the *causal* case but not
the *slot-collision* case: two ops can be causally independent yet contend for the same live
slot. Un-tombstoning without re-checking slot occupancy reintroduces the two-live-rows /
thrown-undo break.

**Fix.** The undo-dependency check must include a **slot-occupancy edge**: undo of `k` is
blocked/cascaded if any later live op (whether or not it read k's output) occupies a live_key
that k's un-tombstone target would re-claim. Equivalently, before un-tombstoning, verify the
target live_key has no current live row; if it does, that later row is a dependent → cascade or
block. Add a test: causally-independent same-live_key ops force an undo cascade, never a thrown
unique-index violation.

---

## PART 4 — SEV-3 / nits

- **R2-8 · Overlay 3-way diff: a re-extracted claim matching a human `remove_from_set` routes
  to review (§5.3 M2), but a re-extraction matching a human `add_to_set` that the human LATER
  undid (snapshot-undo, not a retraction) has no post-dating "removal" record** — the undo
  tombstoned the add but didn't write a retraction the diff can see. The 3-way overlay keys on
  "human retraction/removal post-dating the note edit"; a snapshot-undo of an add is neither.
  Re-extraction silently re-adds the member the human undid. Fix: undo of an `add_to_set` must
  leave a diff-visible suppression marker (or the overlay must consult `undone_by` op history,
  not just live retractions).
- **R2-9 · `assert_distinct` (§4.1 group F) has no defined interaction with the
  `merge_entities` member-dedup pass** — if two members were deduped by an entity merge and a
  later `assert_distinct` splits them, the collapsed member is not re-forked. Documentable;
  rolls into R2-1's member-layer dedup wiring.
- **R2-10 · `valid_from_sortkey` precision normalization (§3.1) is the supersession/"latest
  live" tie-break for functional-over-time current (§3.3), but two stints with the same
  normalized sortkey** (both precision=year, same year — "left Acme and joined Globex in 2023")
  have an undefined "which is current" outcome. Tie-break to reported_at/tx_from, then
  human_touched (mirrors R1 S3-3). Documentable.
- **R2-11 · `structured` shape with an internal typed sub-field** (e.g. `address.region` as an
  enum) — B2b range/co-location guards are specified for top-level quantity/date but not for
  fields *inside* a `structured` value; a corrupt postal/region inside an address bypasses the
  plausibility gate. Documentable; extend B2b recursively into structured fields.

---

## Summary

**R1 verification:** S2-1, S2-2 (named axes), S2-3, S2-4 (named case), S2-5, S2-6, S1-1
(overwrite half), S1-2/S2-8 (gate half) — **CONFIRMED-FIXED** (8). **STILL-OPEN / not
genuinely closed:** **S1-3** (fork leg — R2-1), **S1-4** (interval reconstruction — R2-4),
**S2-7** (slot-collision undo — R2-7). Two "fixes" spawned new bugs: **S2-6→R2-5**,
**S1-2/S2-8→R2-3** (the promotion op is referenced but unbuilt).

**New findings:** SEV-1 ×2 (R2-1 member fork / natural-key-map collision; R2-4 per-interval
history not reconstructable). SEV-2 ×5 (R2-2 set contradiction-review never fires; R2-3
realization op missing from algebra; R2-5 no-abutment vs functional live-index collision;
R2-6 merge_facts provenance/modality laundering; R2-7 undo slot-occupancy not checked).
SEV-3 ×4.

**Recurring root causes (R2):** (1) the modality-in-both-keys model lacks a *modality-stripped*
comparison path, so contradiction detection (R2-2) and the realize op (R2-3) have no firing
mechanism. (2) valid-time "former" vs tx-time "supersede" are conflated (R2-5), and undo's
dependency check models data-dependency but not live-key *slot occupancy* (R2-7) — both stem
from treating the value-excluding functional live_key as fully orthogonal to valid-time when it
is not. (3) member identity is stable against *replace/respell* but not against *mint-race* or
*natural-key-map collision* (R2-1). None of S1-3, S1-4, S2-7 should be marked FIXED at sign-off.

*End R2 (correctness lens). New SEV-1 ×2, SEV-2 ×5, SEV-3 ×4.*
