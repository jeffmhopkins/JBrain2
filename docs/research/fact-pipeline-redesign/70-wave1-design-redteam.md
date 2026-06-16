# Wave 1 — Negation safety: design red-team

Adversarial review of the Wave-1 approach (60-incremental-plan.md) against the **shipped** code
(`analysis/supersession.py`, `analysis/repo.py`, `analysis/canonical.py`, `analysis/pipeline.py`,
`analysis/graph_context.py`). Verdict: the gap is real but **smaller and more lopsided than the
plan assumes** — selection already filters assertion in most read paths, and supersession is
*partly* polarity-aware already. The danger is the plan's blunt "key on assertion everywhere"
breaking the one case where a flipped polarity *must* supersede.

---

## SEV-1 — "assertion in the live-slot identity" silently breaks the negated-correction supersede

`supersession.decide` already routes by polarity in two of three places: `values_equal`
(supersession.py:324) returns False on an assertion flip, and the closed-on-arrival branch is
gated to `assertion == "asserted"` (supersession.py:467-471) **specifically so a NEGATED disposal
falls through and supersedes** the asserted open value ("I no longer own the F-150" must retract
"I own the F-150"). The functional/state head-contest path (supersession.py:530-566) is the ONE
place where a negated candidate today legitimately supersedes the asserted current head.

If approach (a) adds `assertion` to the slot identity used for SELECTION (`_existing_facts`,
pipeline.py:1578-1591) OR makes the head-contest only consider same-polarity actives, then
"I no longer own X" no longer SEES "I own X" → the asserted owns-edge stays `active` + open
forever, and the negation lands as a separate co-equal live head. **This regresses a shipped,
intended behavior** (the `owns→ownedBy` disposal case the inverse-pairs comment calls out, and the
`values_equal` docstring's "would otherwise leave a head asserting the opposite of the truth").

**Fix / revision:** Do NOT add `assertion` to the candidate-retrieval key in `_existing_facts`;
keep loading both polarities for the slot. The correctness rule is narrower: *a non-asserted
candidate must never supersede an asserted head of the SAME value*, but *a negated candidate of the
SAME value as an asserted head is exactly the disposal/retraction that SHOULD win.* So gate inside
`decide`, not in the SELECT: when candidate and current head have **opposite polarity but equal
value/object**, that is a transition (negation retracts the asserted) → supersede as today. When
they have the **same value but the candidate is `hypothetical`/`reported`/`question`/`expected`**
(non-asserted, non-negated) it must NOT displace an asserted head → route to (b). The plan's
phrasing "a negated never supersedes an asserted of opposite polarity" is **backwards for the
disposal case** and must be restricted to the genuinely modal (non-negated) assertions.

## SEV-1 — `current()` = asserted-only would hide the `negated` retraction state on the entity page

`entity_view` (repo.py:638-657) picks `current` = first `status='active' AND valid_to IS NULL` row
**with no assertion filter**. A `negated` "no longer allergic to penicillin" is stored as an
active, open, `negated` fact (that's how a retraction is represented when nothing positive replaces
it). If (c) filters `current` to `assertion='asserted'`, that entity's `allergy.penicillin` slot
shows **no current value and the stale asserted "allergic" as history with nothing live** — i.e.
the page reads as if the allergy were simply forgotten, not actively negated. For a health-safety
surface that is *worse* than today. Same risk in `note_currency` (repo.py:744-766, no assertion
filter) and `canonical.py` name-projection / `corroboration_count` (canonical.py:117, 196 — no
assertion filter; a `negated` name fact is rare but would now count toward auto-confirm).

**Fix:** `current()` must remain three-valued: an asserted open head is "current"; an open
**negated** head with no asserted peer is "currently negated" (render it explicitly, e.g.
`current_negated`), not dropped. Only `hypothetical/reported/question/expected` get hidden from the
current floor. Filtering bare `assertion='asserted'` in `entity_view`/`note_currency` is the
regression; the fix is "asserted OR negated-with-no-asserted-peer."

---

## SEV-2 — most read paths ALREADY filter assertion; (c) is largely a no-op there, and one place would double-filter wrong

`graph_context` (220, 226, 284-filtered downstream), `relate` (repo.py:378), `ego_graph`
(418/424), `full_graph` (511), `entities.py` corroboration (154, 183), and `consolidation.py:74`
all already constrain `assertion='asserted'`. So the plan's "`current()` everywhere" is only a
*new* filter in `entity_view`, `note_currency`, and `canonical` name-projection — exactly the three
places where (per SEV-1) a blunt asserted-only filter is the regression. The plan should scope (c)
to **those three surfaces with the three-valued rule**, and note the graph/agent paths are already
correct (don't re-touch them).

## SEV-2 — contradiction review (b) is under-specified for SET predicates and value-less object edges, and can duplicate cards

(b) says an asserted+negated pair on the same `(subject,entity,predicate,qualifier,value)` routes
to a CONTRADICTION item. But: (1) for a **non-functional relationship** (set-valued, e.g.
`friend`), `decide` returns a bare `insert` (supersession.py:447-448) and accumulates — an asserted
`friend→X` plus a negated `friend→X` are two co-equal live edges today; (b) needs a path here or
the "unfriend" contradiction is never filed. (2) For a **pure object edge** there is no `value` —
the identity is the `object_entity_id`; the plan's `(…,value)` tuple must fall back to the object.
(3) **Idempotency/loop risk:** re-analysis re-extracts both the asserted and negated fact every
run. `_insert_held_fact` refreshes an existing pending_review row for the identity key in place
(pipeline.py:1677, 1740), but the *contradiction* card is a NEW kind — it must dedupe on the same
`(entity,predicate,qualifier,object/value)` or each re-ingest (D1) files a duplicate card. The
existing `attribute_collision`/`fact_conflict` paths hold BOTH sides and key the card on
`conflicting_id`; (b) should reuse that machinery (a `fact_conflict` variant) rather than invent a
card with no dedupe.

## SEV-2 — re-ingest (D1) retroactively flips "current" for the existing corpus

Existing rows already have `assertion` populated. Under today's polarity-blind head-contest, some
slots currently show an asserted value that a later **negated** note already (silently, per the
bug) superseded — or vice-versa. After Wave 1 + D1 re-ingest, those slots will re-resolve: this is
the *intended* correctness fix, but it means entity pages and the display-name projection
(`canonical.py` reprojects `canonical_name` on the current asserted head) can **change visible
names/values** for the existing corpus with no human action. Acceptable per D1, but flag it:
re-ingest must run the name-reprojection (canonical.py:150) so a slot whose head flipped doesn't
strand a stale `canonical_name`, and the run-log should diff changed current-heads for owner review.

## SEV-3 — `pinned` and `derived_from_fact_id` interactions hold, but verify in tests

A `pinned` asserted head is re-flagged not flipped (supersession.py:537-544) — a negated candidate
against a pinned asserted head must route to review, not supersede; the existing pinned guard covers
this *if* (a) stays out of the retrieval key (per SEV-1 fix). Derived inverse shadows
(`derived_from_fact_id`) carry their source's `assertion`; the shadow-cascade in `resolve_review`
(repo.py:1159-1192) and `_update_shadows_in_place` already mirror lifecycle, so a negated primary's
shadow must also be negated — confirm the reciprocal materialization (pipeline.py:2270+) copies the
candidate `assertion` (it reads `fact.assertion` at 2275/2334, so it does) and that the
asserted-only graph reads (which already filter assertion) correctly drop a negated shadow edge.

## SEV-3 — no new table ⇒ the plan's "RLS isolation test" line is mostly N/A

Wave 1 adds no table (only an index + a possible new review `kind`, which lives in the existing
`review_items`, already RLS-tested). The "every new table ships its RLS test" obligation doesn't
bite; the real RLS surface is that `_existing_facts` runs as owner `SYSTEM_CTX` (pipeline.py:1574)
with an explicit `domain_code` filter — adding assertion logic must **not** drop that domain filter,
or a negated health fact could contest a same-key general fact across the firewall. Test: a negated
candidate in `general` must not see/supersede an asserted `health` head of the same address.

---

## Revised Wave-1 approach

1. **Selection key unchanged.** `_existing_facts` keeps loading **both** polarities for the slot
   (do NOT add `assertion` to the retrieval key or the domain filter). Polarity decisions happen
   inside `decide`, where the value/object is known.
2. **Supersession rule (narrow):** in the head-contest and attribute/state branches —
   - opposite-polarity, **same value/object** (asserted↔negated) = a transition → supersede as
     today (preserves the disposal/retraction the shipped code already does);
   - candidate is **modal** (`hypothetical/reported/question/expected`) and would displace an
     `asserted` head → never supersede; file the contradiction/conflict item instead;
   - reuse the existing `fact_conflict` machinery (hold both sides, key on `conflicting_id`) so the
     card dedupes across re-ingest; extend it to set-valued edges (asserted+negated `friend→X`) and
     to value-less object edges (key on `object_entity_id`).
3. **`current()` three-valued, scoped to the 3 unfiltered surfaces only** (`entity_view`,
   `note_currency`, `canonical` name/corroboration): "current" = asserted-open head, OR a
   negated-open head with no asserted peer (rendered explicitly as *currently negated*). The
   graph/agent/consolidation paths already filter `assertion='asserted'` — leave them.
4. **D1 re-ingest:** re-run name reprojection; run-log diffs changed current-heads for owner review.
5. **Tests:** negated disposal still supersedes the asserted edge; a `reported`/`hypothetical`
   value can't displace an asserted head (→ conflict card); negated-open with no asserted peer is
   shown, not hidden; set-valued unfriend files a card and dedupes on re-ingest; cross-domain
   negated candidate can't contest an asserted head behind the firewall.
