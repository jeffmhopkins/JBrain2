# Red-team R1 — Correctness & edge cases (fact-pipeline-redesign)

**Lens:** correctness & edge cases. **Target:** `20-spec-v0.md` (synthesis v0),
against `00-framing.md` (wishlist §2, invariants §4). Briefs A–G consulted for detail.
**Posture:** adversarial. The job is to find concrete fact/edit sequences that produce a
**wrong or ambiguous graph state**, not to praise the design.

Severity key: **SEV-1** breaks an invariant or a core goal · **SEV-2** serious, must-fix
before sign-off · **SEV-3** nit / documentable accepted-risk.

---

## SEV-1 findings

### S1-1 · Negation & a later *retraction of the negation* silently re-asserts a fact into "current"

**Where:** §2.6(iv) negation; §3 `current()` selection; §2.5 `modality` carried but not
keyed.

**Scenario.** Two notes about Sam's penicillin allergy:
1. Note A: "Sam is NOT allergic to penicillin." → `health.allergy`, value
   `text:"penicillin"`, `modality:"negated"`, `kind:"state"`. Committed.
2. Note B (later): "Sam's penicillin allergy was confirmed." → same slot, value
   `text:"penicillin"`, `modality:"asserted"`.

Both rows share `slot_key = hash(owner, subject, predicate, qualifier, domain
[, value_identity])`. **`modality` is NOT in the slot key** (§3.1) and **not in
`value_identity`** (§3.2: object-id / natural-key / minted-uuid — modality is absent).
So the asserted row and the negated row collide on the *same* member identity for a
set predicate, or on the same functional key. The negated "not allergic" and the
asserted "allergic" are treated as **the same fact over time**, and newest-wins
supersession silently drops one. Worse: if `health.allergy` is a *set* predicate
(the §7(j) default), `value_identity` is derived from the value `"penicillin"`, which
is identical, so the asserted row supersedes the negated row as if it were a typo-fix
of the *same* member — exactly the "supersede that member, not a spurious add"
mechanism in §3.2, now misfiring to flip a negation into an assertion.

**Why it is SEV-1.** The spec's own zero-tolerance gate is "negated/hypothetical →
asserted" (D §5, §5 eval). Here no LLM mis-extraction occurs; the *storage identity
model* itself collapses an asserted and a negated claim about the same value. A
"not allergic to penicillin" fact can be silently overwritten by — or silently
overwrite — an "allergic to penicillin" fact, and `current()` (§3) has no modality
filter at all (it selects on `tx_to IS NULL`, valid-time, supersession; modality is
never consulted). **A negated fact can therefore leak into current-value**, or an
asserted allergy can be erased. In the health domain this is a safety-critical wrong
state.

**Fix / open question.** (a) `modality` MUST participate in the slot key (or in
`value_identity`) so an asserted and a negated claim about the same (subject,
predicate, value) are *distinct slots that conflict*, not the same slot that
supersedes. (b) `current()` MUST filter `modality = 'asserted'` — negated /
hypothetical / question / expected / reported rows must be excluded from current-value
by construction, not by hoping they never share a key. (c) An asserted-vs-negated
collision on the same value is a **contradiction review item** (like the Allen
overlap conflict, E4), never a silent supersession. Open: is "not allergic" a *fact
with modality* or the *absence* of the allergy fact? The spec must pick; today it is
both and they collide.

---

### S1-2 · `current()` ignores modality entirely — hypothetical/reported/question facts can surface as truth

**Where:** §3 "Live-row selection" and §3 current-value text; §2.1 envelope lists
`modality` but §3.1 stores it as a plain column never referenced in selection.

**Scenario.** "If I switch to Acme next year" → §2.6(iv) emits
`person.employer = Acme`, `modality:"hypothetical"`, `valid_from:2027`,
`confidence:0.3`. The spec says it is "carried, not asserted into the live floor until
promoted" — but **there is no mechanism described that keeps it off the live floor.**
It is committed as a `fact_assertion` row with `state='live'`, `tx_to IS NULL`. Once
2027 arrives, `valid_now` is true (`valid_from <= now`, `valid_to` open), it is `live`,
it is not superseded → `current(self, person.employer)` returns it. The hypothetical
becomes the current employer with zero human action.

Same hole for `modality:"reported"` ("Mom *says* I'm allergic to shellfish") and
`modality:"question"` ("am I still on metformin?") — all are `live` rows that
`current()` will happily return.

**Why it is SEV-1.** "Does a negated or hypothetical fact ever leak into current?" —
**yes, by default**, because the live-floor / current-value machinery filters on
tx-time, valid-time, and supersession but **never on modality**. The §2 spine claim
"the extraction LLM emits structured intent only" does not help: a structurally valid
hypothetical row is indistinguishable to `current()` from an asserted one.

**Fix.** Make modality a *first-class gate* in three places: (1) the live-row partial
index / current-value query MUST restrict to `modality='asserted'`; (2)
non-asserted rows live in a separate logical floor ("carried" / candidate), and
promotion (hypothetical→asserted, expected→asserted on realization) is an explicit
`set_modality` op (it exists in the op-kind list §3.2) that re-keys into the live
floor; (3) define what `valid_from` even *means* on a hypothetical — a future-dated
hypothetical must not auto-promote when its date arrives.

---

### S1-3 · Set-member identity drift across reprocessing resurrects removed members and forks corrected ones

**Where:** §3.2 `value_identity` priority list (object-id → natural key → minted
uuid); C risk 4 ("members carry stable identity tied to (subject, predicate,
value-hash)"); the spec does NOT carry C's mitigation forward.

**Scenario A (resurrection).** Set predicate `person.phone`, value identity = natural
key (E.164). Member `+1-555-0100` exists. Human `remove_from_set` tombstones that
`value_identity`. Note is re-ingested (re-analysis migration, D §4, or just a second
note quoting the same number). The extractor re-emits `+1-555-0100`; its
`value_identity` re-derives to the *same* E.164 natural key. The committer sees a new
asserted member with a `value_identity` whose only live row is a tombstone. **Does a
new `add` resurrect the removed member, or is it suppressed by the tombstone?** The
spec never says. If add wins, the human's removal is silently undone on every
reprocess. If tombstone wins, a legitimately re-added number can never come back.

**Scenario B (fork).** Set predicate `person.child`, value identity = **object
projection id** (priority 1). Two notes mention "Lydian." First pass mints
`ent_lydian_1`; entity resolution on the second pass (different context) mints
`ent_lydian_2` before a later `merge_entities`. Now the *same child* has two
`value_identity` values → two live set members → the daughter appears **twice** in
current-value. A subsequent `merge_entities(ent_lydian_1, ent_lydian_2)` resolves the
*entity* but the spec's `value_identity` is the **projection id stored in the slot
key (bytea)** — merging entities does not re-key the two assertion rows, so the
duplicate set members persist. §3.2 claims "re-spelling Acme's name does not fork the
set" — true for *re-spelling*, but **entity-resolution churn across passes DOES fork
it**, and entity merge does not heal it.

**Why it is SEV-1.** Wishlist §2.9 (the override-vs-array core) and the framing's
"member identity drift across reprocessing" are exactly this. The spec adopted
`value_identity` = object-id (B §3.2 priority 1) but **dropped C's value-hash +
pin-protection mitigation** and never defined remove/tombstone interaction with
re-add or entity-merge interaction with slot re-keying. The result is duplicated and
resurrected set members — a wrong graph state on the most-emphasized requirement.

**Fix.** (a) Define tombstone-vs-readd precedence explicitly: a re-extracted member
matching a *human* `remove_from_set` tombstone must route to review, not auto-add
(human intent beats reprocess, mirroring the pinned-vs-reprocess rule §3.2). (b)
`merge_entities` MUST trigger a slot re-key / member-dedup pass: any two live set
members whose `value_identity` projection-ids now resolve to the same canonical entity
collapse to one (with audited supersession), else the merge is incomplete. (c) Carry
C risk-4's stability rule into the spec as binding, not just a brief.

---

### S1-4 · Functional-now vs functional-over-time (§7(i)) is unresolved *and* the two readings give different current-value — a chosen-default coin-flip on every "former X"

**Where:** §7(i) provisional "functional with temporal succession of supersessions";
§3 `current()`; G §4; the storage `slot_key` excludes value for functional (§3.1).

**Scenario.** `person.employer` declared **functional** (the §7(i) reading), Sam.
1. Note A: "Sam works at Acme since 2019" → functional slot, value Acme,
   `valid_to:open`.
2. Note B: "Sam now works at Globex (since 2023)" → **same functional slot_key (value
   excluded)** → new value supersedes. Per G §4 supersession, the *prior* Acme row's
   `valid_to` is closed at 2023 (abutment). Good so far.
3. Human correction: "actually he went back to Acme in 2025." Issues… what? On a
   functional predicate the only legal value-op is `set_field value` or `supersede`
   (C §3 table). `supersede` writes Globex→Acme. But now Acme has **two** historical
   intervals ([2019,2023) and [2025,open)). The functional slot_key **excludes the
   value**, and the live partial index allows **≤1 live row per slot** (§3.1). The
   [2019,2023) Acme row is `superseded` (not live), the [2025,open) Acme row is live.
   Fine — *until* a query asks "every interval Sam worked at Acme": the two Acme rows
   have the same subject+predicate but are **not** grouped (functional key excludes
   value, so they share a key with the *Globex* row too). Acme's two stints and the
   Globex stint are all the *same slot*, distinguished only by `supersedes` chain and
   valid-time. Reconstructing "Acme: 2019–2023 and 2025–present; Globex: 2023–2025"
   requires walking the supersession chain and re-segmenting by value — which the spec
   never specifies and the slot abstraction actively hides.

Contrast the §7(j) default: if `person.employer` were **set** (ambiguous-cardinality
→ set), the three stints are three members with distinct `value_identity`, history is
clean, but "who is the *current* employer" requires a `valid_now` filter the §3
set-valued `current()` returns as a **set** (all live members) — and a person with
overlapping-in-time set memberships (two jobs) is now indistinguishable from the
buggy double-Acme.

**Why it is SEV-1.** The same predicate gives **materially different and partly-wrong
history reconstruction** depending on the unresolved §7(i) pick, and *neither* pick is
fully correct: functional loses per-value interval history (it's hidden behind the
value-excluding key); set loses the "exactly one current" guarantee and conflates
"two concurrent jobs" with "drift bug." This is the framing's "conflicting temporal
intervals / Allen relations" + "current-value derivation" hitting at once. Shipping
either default silently mis-models the *most common* corrected fact (employer/home).

**Fix / position.** See §7(i) position below. Minimally: a functional-over-time
predicate needs the slot key to include value *for history grouping* while the *live*
index excludes it (two indices, one identity), so "all Acme intervals" is a clean
query AND "exactly one current employer" holds. The spec currently has one key doing
both jobs and it cannot.

---

## SEV-2 findings

### S2-1 · Split → re-extract round-trip is not idempotent; the human split is lost on reprocessing

**Where:** §2.1 `split_group`; C `split_fact`/`merge_facts`; D §4 re-analysis; no
binding between `split_group`/op-log and the next extraction pass.

**Scenario.** Extractor emits ONE fact "my daughters Summer, Harmony, Lydian" as a
single `text` value (it failed to split). Human issues `split_fact` → three
`person.child` members. Later the note is re-extracted (contract major bump, D §4).
The new pass *also* emits one combined fact (same model failure) OR now correctly
emits three. Either way, the re-extracted facts have **new `claim_id`s and no link to
the human's `split_fact` op**. There is no described mechanism that says "this sentence
was already split by a human into these three members; re-extraction must reconcile
against that." Result: duplicate children (old split members + new extracted members)
or a resurrected combined fact alongside the split. The op-log is "authoritative
history" (C §5) but reprocessing writes through a **non-op path** (the committer
applies extractor proposals) — C open-Q 4 names exactly this drift and the spec does
not close it.

**Fix.** Re-extraction must be a *diff against existing slots keyed by provenance span*
(D mentions shadow+diff for migration but not for member-level reconciliation): an
extracted claim whose provenance span overlaps a span already covered by human
structure ops (`split_fact`, `merge_facts`, `add_fact`) routes to review rather than
blind-committing. Pin-protection (§3.2) protects *pinned* facts but split children
aren't necessarily pinned.

### S2-2 · `merge_facts` across differing temporal/modality/domain has undefined value-and-time semantics

**Where:** C `merge_facts` ("union provenance spans"); §2 envelope carries per-fact
temporal/modality/domain; spec never says what the merged fact's temporal/modality is.

**Scenario.** Merge two facts: A = `home=Austin [2018,2021)` asserted, B =
`home=Austin [2019,open)` reported/hypothetical. `merge_facts` unions provenance — but
which `valid_from`? Which `valid_to`? Which `modality`? If it takes A's, B's added
evidence (the open end) is lost; if it takes the union interval, a `meets`/`overlaps`
Allen conflict is silently swallowed. Merging across `domain` is worse: union of a
general and a health provenance span on one row **violates F §2.4 (provenance stays
same-domain)** and the §8 "same-domain provenance" invariant. The op is in the
algebra but its temporal/modality/domain reconciliation is unspecified → merges
produce arbitrary or firewall-violating state.

**Fix.** `merge_facts` must (a) reject cross-domain merges outright (firewall), (b)
require explicit temporal + modality resolution in the op args (no heuristic, mirroring
C's "explicit maps" rule for `split_entity`), (c) be rejected when the inputs are in a
non-trivial Allen relation that would fabricate an interval.

### S2-3 · Recurrence exception/override identity breaks when the rrule or dtstart is later edited

**Where:** §2.6(v) recurrence; G §2.3 `exdates`/`overrides` keyed by
`recurrence_id` = original instant; G op `set_recurrence` "attaches/replaces".

**Scenario.** "PT every Tue/Thu through Dec, skip Sep 8 (exdate), move Mar 17→18
(override keyed recurrence_id=2026-03-17)." Human then `set_recurrence` to fix the
rule: "actually it was Mon/Wed, starting Jan 5." The `exdates=[2026-09-08]` and
`overrides[recurrence_id=2026-03-17]` were keyed to the **Tue/Thu** instance set. After
the rule change, 2026-09-08 (a Tuesday) and 2026-03-17 (a Tuesday) are **no longer in
the recurrence set at all**. The exception and override are now **dangling** — they
silently match nothing, so "skip Sep 8" is silently lost, and the moved session
vanishes. G §2.3 says `set_recurrence` "attaches/replaces" the recurrence object but
the spec never says it must reconcile or invalidate existing exdates/overrides.

**Why SEV-2.** A human's "skip this one" correction silently evaporates on an unrelated
rule edit → wrong schedule, no error, no review flag. Recurrence + exceptions is an
explicit framing target.

**Fix.** `set_recurrence` must validate every existing `exdate`/`rdate`/`override`
`recurrence_id` against the *new* rule and either re-anchor, drop-with-audit, or route
to review — never silently retain dangling keys.

### S2-4 · `replace_head` + valid-time = a member with two live versions or a lost interval

**Where:** C `replace_head` ("supersede the member, add successor linked via
superseded_by"); §3.1 live partial index "≤1 live row per member (key includes
value_identity)"; §3.2 "replace reuses the member's existing value_identity."

**Scenario.** Set predicate `person.phone`, member `+1-555-9999` (value_identity
`vi_03`). `replace_head(vi_03, "+1-555-0100")`. §3.2 says replace *reuses* the same
`value_identity`. But the new value is a *different phone number* whose natural-key
`value_identity` (E.164) would be `vi("+15550100")` ≠ `vi_03`. So which identity does
the successor row carry — the **old** member's `vi_03` (per C/§3.2 "reuses"), or the
**new value's** natural key (per §3.2 priority 2)? If it keeps `vi_03`, the
`value_identity` no longer matches the value's natural key → a future re-extraction of
`+1-555-0100` derives `vi("+15550100")`, doesn't match `vi_03`, and **adds a duplicate
member** (the corrected number now appears twice). If it adopts the new natural key,
the supersession link crosses two different slot members and the "≤1 live per member"
index is keyed differently before/after → the old member never gets its `tx_to` closed
under the same key and **both versions stay live**.

**Why SEV-2.** Direct internal contradiction: "replace reuses value_identity" vs
"value_identity = natural key of the value." For natural-key members these cannot both
hold. Produces either a duplicate or two live rows.

**Fix.** State the rule precisely: `replace_head` mints a successor under a **stable
minted** `value_identity` (priority 3) decoupled from the value's natural key, and the
old natural-key→member mapping is updated; OR replace = remove+add with an explicit
"supersedes" provenance link and the duplicate-on-reingest case is handled by S1-3's
re-extraction diff.

### S2-5 · Messy/locale value typing: parser-wins (§7(c)) silently corrupts, and the "review escape" can't fire when parser and registry agree wrongly

**Where:** §2.3 / §7(c) "parser re-derives and wins ties; hard disagreement → review."

**Scenario.** Note: "BP was 120/80." Registry `value_shape` for `health.blood_pressure`
is `structured(shape:bp)`. Model emits `type:text raw:"120/80"`. Deterministic parser
re-derives a structured `{systolic:120, diastolic:80}`. Fine. Now: "weight 12 stone 4"
or "blood sugar 5,4" (European decimal comma) or "due 03/04/26" (D/M/Y vs M/D/Y). The
parser re-derives `5.4` from `5,4`? Or `54`? Or `5` (comma as thousands)? The model
*emitted* the right `raw`, the parser's locale assumption decides, and because parser
**wins ties**, a wrong parse that is *internally consistent with the registry shape*
(a number was produced, shape matches) **does not trigger the model-disagreement→review
escape** — the escape only fires on parser/model *disagreement about the variant*, not
on a same-variant numeric misparse. §7(c)'s own flip condition ("parser silently
corrupts in the same direction as a missing test") is realized here and is **not
caught** by the stated escape.

**Why SEV-2.** A health/finance quantity can be silently off by 10× (comma) or a date
off by months (D/M/Y), with no review flag, because both parser and registry agree on
the *shape* while disagreeing with reality. The eval's per-field semantic metric (D §5)
might catch it on the golden set but production locale drift won't be gated.

**Fix.** The review escape must also fire on **low-confidence parses within a variant**
(ambiguous locale, unit coercion that changed magnitude, date with ambiguous
field-order) — not only on variant disagreement. Carry locale/units as explicit
registry context so the parser is not guessing.

### S2-6 · Allen `meets` auto-abutment fabricates a boundary the source never asserted

**Where:** G §4 supersession ("if new provides start, old `valid_to` becomes
closed(new.valid_from) — abutment"); G E4; spec §3 inherits this.

**Scenario.** Note A: "lived in Austin." (`from:2018, to:open`). Note B: "moved to
Denver in 2021." Supersession auto-closes Austin's `valid_to = closed(2021)`. But the
source **never said Austin ended in 2021** — only that Denver *started* in 2021. The
person could have kept the Austin place. The system has now **fabricated** a closed end
date `2021` for Austin and stamped `certainty:asserted` on it (the row's
`valid_to_certainty` default is `'asserted'`, §3.1). G's own G-VAGUE-2 forbids
inventing an endpoint; abutment violates it for functional predicates. The fabricated
`closed(2021)` then feeds `current()` as ground truth.

**Why SEV-2.** Direct tension with the anti-fabrication thesis (the whole point of the
bound trichotomy). Auto-abutment is convenient but it manufactures a dated boundary
from an inference. At minimum the fabricated bound must carry `certainty:"inferred"`,
not `"asserted"`, and for `overlaps`/`during` it must go to review (G R6) — but the
spec's §3 doesn't carry the certainty distinction into the abutment write.

**Fix.** Auto-abutment writes `valid_to.certainty="inferred"` (or `bound="unknown"` →
"former" when the new start is itself imprecise), never `"asserted"`; reserve
`asserted` ends for source-stated ends. Surface the inferred boundary in rendering.

### S2-7 · Bitemporal undo of a supersession can leave two live rows or a gap

**Where:** §8 "Watch: undo composition of structure+identity ops"; G E8/§4; C
`supersede` undo "drop successor + un-supersede"; §3.1 live partial index.

**Scenario.** Functional employer: Acme[2019,open) live. Supersede with Globex[2023,
open) → Acme `valid_to` closed at 2023, Globex live. Then a *second* op retimes Globex
(`set_bound from 2024`). Now undo the *first* (supersede) op using its stored inverse
("re-assert Acme open, drop Globex"). The inverse was precomputed against the state at
apply time (Globex from=2023) but the world moved (Globex from=2024, possibly other
members added). Dropping Globex and re-opening Acme: does Acme's `valid_to` restore to
`open`? The inverse stored Acme's pre-image (`open`), so yes — but the *intervening
retime* of Globex is now orphaned, and if Globex was also referenced by a later op the
undo composition is undefined (C open-Q 8, acknowledged but unresolved). The
optimistic-concurrency `preconditions` (C §6.7) guard *apply*, but a stored inverse
replayed after intervening ops can still violate the ≤1-live-per-slot index (re-opening
Acme while a sibling row is live).

**Why SEV-2.** Reversibility is a binding invariant (§4). The spec acknowledges the
composition risk as a "watch" but ships no rule. A failed/partial undo that leaves two
live functional rows is a silent invariant break (the partial unique index would
actually *reject* it at write time → the undo throws, leaving the op-log in a state
where the documented "every change is unwindable" is false for this sequence).

**Fix.** Undo must re-validate the inverse against current state (not blind-apply),
and a non-composable undo (intervening dependent ops) must be **blocked with an
explicit dependency error** (undo-blocked-by graph, C open-Q 8) rather than producing
a wrong/failed write. Define this, don't "watch" it.

### S2-8 · `scheduled` future-dated facts and `expected` modality both auto-flip to current with no realization step

**Where:** G §2.4 status `scheduled`; §2.1 modality `expected`; §3 `current()` uses
`valid_now`.

**Scenario.** "Dentist 2026-08-01" → `scheduled`. "I expect to start at Acme in Aug"
→ `modality:expected`. Both have future `valid_from`. When 2026-08-01 passes, status
recomputes (G §2.4) and `valid_now` becomes true. For the dentist (an `event`) that's
arguably fine, but for `expected`-modality employer it **auto-asserts** that Sam works
at Acme with no confirmation the expectation was realized — same hole as S1-2 but via
the time axis. The plan/forecast becomes recorded truth by the mere passage of time.

**Fix.** `expected`/`scheduled` → `asserted`/`occurred` must be an explicit promotion
op (or a realization confirmation), never an implicit consequence of `now` crossing
`valid_from`. Tie to S1-2's modality gate.

---

## SEV-3 / nits

- **S3-1 · `value_identity` is `bytea` in the slot key but entity merge changes which
  canonical it denotes** — the key is immutable, the meaning isn't. Re-keying on merge
  (S1-3 fix) needs a defined migration, not a hash that silently goes stale.
- **S3-2 · `era`/`decade` precision comparisons in `valid_now`** (G R1, "is 2019 <
  2019-06?") use start-of-window for `from`, end-of-window for `to`. An `era`
  ("childhood") `from` with start-of-window = birth could make a childhood fact
  "current" at the wrong boundary. Boundary tests owed; documentable.
- **S3-3 · `confidence` not in any key but used as a supersession tie-break** (G §4
  `argmax`). Two re-extractions of the same fact with jittered confidence could flip
  which row is "current" on reprocess with no semantic change. Tie-break should prefer
  human-touched / pinned, then reported_at, before confidence.
- **S3-4 · `add_fact` with `source_kind:human_assertion` citing the op as provenance**
  fails B1 span verification by construction (no quoted span) — it must be on the same
  exemption path as inferred facts (§7(d)), which the spec implies but doesn't wire.
- **S3-5 · `reported`-modality (hearsay) has no distinct current-value treatment** — it
  shares the asserted floor. "Mom says I'm allergic" and "I am allergic" should not be
  equally authoritative; reported should be candidate, not current (rolls into S1-2).

---

## Positions on the §7 open conflicts

**(b) one-claim-per-value vs one-record-N-cells.** **Endorse (i): storage is strictly
one-edge-per-value; the card's "cells" are pure presentation that lower to
member-targeted ops.** This is correct and non-negotiable — a value-array storage shape
*is* the override-vs-array bug (B §7-C). **But** the red-team flips the load-bearing
caveat: the spec must prove each cell serializes cleanly to a `value_identity`-targeted
op *including* `replace_head`+valid-time (S2-4) and split/merge of a cell (S2-1/S2-2).
Until S2-4 is fixed, the cell→op lowering is **not** clean, so (b)'s provisional pick is
correct in principle but *unproven* in practice. Conditional endorse.

**(i) functional-over-time vs functional-now.** **Reject the provisional "functional
with temporal succession" as stated — it is under-specified and loses per-value
interval history (S1-4).** Position: a functional-*over-time* predicate needs **two
identities** — a *history* grouping key that **includes** value (so "all Acme stints"
is one clean group) and a *live* selection that enforces exactly-one-current
**excluding** value. One value-excluding `slot_key` cannot do both. If forced to a
single pick, choose **functional-now backed by one-edge-per-value (set storage) plus a
derived "current = latest live by valid_from" view**, because that preserves clean
interval history and makes "current employer" a *derivation*, not a storage privilege —
at the cost of needing a real `valid_now` + supersession derivation for "the one
current." The spec's current single-key functional model mis-models the most common
corrected fact.

**(j) ambiguous-cardinality default → set.** **Endorse `set` as the safer default** —
silent-replace (functional default) is the more dangerous failure and additive
pollution is reviewable/removable. **But** condition it on fixing S1-3 (member drift)
and S1-1/S1-2 (modality in the key/current-value): defaulting to `set` *multiplies* the
member-identity-drift and negation-collision surface, so `set` is only safe once
`value_identity` includes modality and survives entity-merge/reprocess. Endorse with
those as blocking prerequisites.

**(k) structured value variant — closed vs model-coined.** **Endorse closed,
registry-declared shapes** — model-coined structured shapes reintroduce
schema-unconstrained output (defeats D's constrained-decode reliability story and the
value-shape gate) and make the deterministic parser (§7(c)) impossible to write per
shape. A shape-mismatch / unregistered shape must route to **review with a proposed new
shape**, never auto-coin. The only refinement: provide a fast registry-extension path
(a `propose_shape` review item) so "closed" doesn't become a productivity wall — but the
default and the storage contract stay closed.

---

*End R1 (correctness lens). SEV-1 ×4, SEV-2 ×8. The recurring root cause across S1-1,
S1-2, S2-8, S3-5 is that **modality is carried but never gates identity or
current-value**; across S1-3, S2-1, S2-4 it is that **member identity is not stable
across reprocessing/replace/merge**. Both must be closed before sign-off.*
