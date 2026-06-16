# Track E — Review architecture & UX

**Status:** Phase-1 fan-out brief (greenfield, first-principles). Consumes Track C's
operation algebra; coordinates with A (IR), B (storage), G (temporal).
**Owner:** Track E researcher.
**Scope:** the review-item model, the card-as-structured-editor IA, kind-zoo collapse,
batching, decision + audit trail, and the edit-submission contract. No code.

---

## 0. Thesis in one paragraph

Today review is a **menu of verdicts** bolted onto a **zoo of bespoke item "kinds."**
Each kind ships its own card, its own accept/reject affordance, and at most a single
override. The §2 wishlist cannot fit through that door: you cannot add a second employer,
relink the object entity, or retime a fact when the only verb the UI knows is "accept."
The redesign inverts the model: **the review item is one fact record, and the card is a
structured editor over that record.** Every field is an in-place editor; arrays expose
add/replace/remove; structure ops (split/merge/add-missing) are first-class. The human
does not "pick a verdict" — they **edit the fact and submit a decision**, and the edits
serialize to **typed correction operations** (Track C's algebra) that the system applies
as audited, reversible, machine-authored changes. The kind zoo collapses into **one card
shape with conditional sub-editors keyed off `kind` and predicate cardinality**, preserving
the #7 doctrine: the human never hand-writes graph state; they emit operations the machine
applies.

---

## 1. Proposal

### 1.1 Design principles

1. **One item type: `ReviewItem(fact_view)`.** Appointments, lab rows, conflicts, and
   extraction corrections are not separate item *types*; they are facts (or fact clusters)
   with a `kind` and a `reason` for surfacing. The card renders the same skeleton for all.
2. **The card IS the editor.** No "edit mode" toggle, no separate modal per field. Each
   field renders as a display-with-affordance that becomes an inline editor on focus. This
   is the single biggest lever against clicks and cognitive load (per HITL review-UI best
   practice: actions where hands already are, context shown, distractions hidden).
3. **Edits are operations, not a replacement record.** The card tracks a **dirty diff** as
   a list of typed ops (Track C). Submitting flushes the op list as one **decision**. This
   gives per-field audit, partial reversibility, and a clean #7 story.
4. **Override-vs-array is a property of the predicate, surfaced in the UI.** The card knows,
   from the predicate registry, whether a predicate is **functional** (single-valued,
   supersede) or **set-valued** (accumulate). Functional predicates render a single value
   slot with "replace"; set-valued predicates render an array with explicit per-row
   add/replace/remove. The human never has to guess.
5. **Reason-coded decisions.** Every reject/retract/supersede carries a reason code (misread,
   wrong-entity, hypothetical, duplicate, out-of-scope, stale). This is the "why, not just
   the what" that makes the audit trail and any future extractor-eval useful.
6. **Nothing is destructive at the card.** Submitting produces operations; operations are
   applied transactionally and are reversible (reopen/undo). The card's "reject" drafts a
   correction note (per existing doctrine) *in addition to* emitting a retract op.

### 1.2 What the human can do to a card (maps 1:1 to §2 wishlist)

| Wishlist | Card affordance | Emits op (Track C) |
|---|---|---|
| 1 Predicate | predicate picker (canonical/drift/coin) + qualifier field | `set_predicate`, `map_drift`, `coin_predicate`, `set_qualifier` |
| 2 Value | typed value editor (enum/quantity/date/struct/text) | `set_value` |
| 3 Subject relink | entity picker on subject chip | `relink_subject` |
| 4 Object relink | entity picker on object chip (existing/mint/unlink) | `relink_object`, `mint_entity`, `unlink_object` |
| 5 Temporal | temporal sub-editor (from/to/precision/rrule/reported) | `set_valid_from`, `set_valid_to`, `set_precision`, `set_recurrence`, `set_reported_time` |
| 6 Modality | modality segmented control | `set_modality` |
| 7 Domain | domain selector w/ firewall-consequence dialog | `set_domain` (guarded; Track F) |
| 8 Kind | kind selector (reshapes sub-editors) | `set_kind` |
| 9 Cardinality | array rows: add / replace-head / remove | `add_to_set`, `replace_value`, `remove_from_set` |
| 10 Split/merge | split control (1→N), merge tray (N→1) | `split_fact`, `merge_facts` |
| 11 Add missing | "+ Add fact" blank card in same editor | `add_fact` |
| 12 Identity ops | entity-detail relink → split/merge/distinct-from | `split_entity`, `merge_entities`, `assert_distinct` |
| 13 Drop/retract/supersede | decision footer: reject / retract / supersede | `retract`, `supersede` |
| 14 Pin/confidence | pin toggle, confidence acknowledge/adjust | `pin`, `set_confidence` |
| 15 Provenance | source-span chip, re-anchor to span/note | `set_provenance` |

Every wishlist row is expressible; none requires a free-text correction note as the
*only* path (notes remain available as an escape hatch and are auto-drafted on reject).

---

## 2. Concrete schemas (JSON)

Two contracts: the **review-payload** (server → card) and the **edit-submission**
(card → server). The payload is a *projection* of the storage fact (Track B) enriched with
everything the card needs to render editors without extra round-trips (predicate metadata,
candidate entities, enum domains). The submission is a **decision wrapping an ordered op
list** (Track C's algebra). The two are intentionally asymmetric: a fat read, a thin write.

### 2.1 Review payload (server → card)

```jsonc
{
  "schema_version": "review-payload/1",
  "review_item": {
    "item_id": "ri_01J…",            // stable id for this surfacing
    "reason": {                       // WHY this is in the inbox
      "code": "low_confidence",       // low_confidence | conflict | new_entity |
                                      //   domain_move_proposed | split_candidate |
                                      //   appointment_proposed | reprocess_diff
      "detail": "confidence 0.42; conflicts with fact_5567",
      "related_item_ids": ["ri_…"]    // siblings in a batch/cluster (conflicts, splits)
    },
    "priority": 2,                    // 1..3, drives queue ordering
    "opened_at": "2026-06-16T12:00:00Z",
    "batch_key": "note_8841"          // groups items from one note/cluster for batch review
  },

  "fact": {                           // the editable record — projection of storage
    "fact_id": "fact_5567",           // null when reason=add_fact (new blank card)
    "status": "proposed",             // proposed | active | superseded | retracted
    "kind": "relationship",           // event|measurement|state|attribute|preference|relationship
    "domain": "general",              // general|health|finance|location
    "modality": "asserted",           // asserted|negated|hypothetical|reported|question|expected
    "confidence": 0.42,
    "pinned": false,

    "subject": {
      "entity_id": "ent_sam_01",
      "label": "Sam Rivera",
      "type": "person",
      "candidates": [                 // for relink picker; precomputed by resolver
        {"entity_id": "ent_sam_01", "label": "Sam Rivera", "score": 0.91,
         "disambiguator": "brother"},
        {"entity_id": "ent_sam_07", "label": "Sam Patel", "score": 0.38,
         "disambiguator": "coworker"}
      ]
    },

    "predicate": {
      "id": "pred_employed_by",
      "canonical": "employed_by",
      "qualifier": null,              // e.g. {audience:"family"} for nickname predicates
      "cardinality": "set",           // "functional" | "set"  ← drives override-vs-array
      "value_shape": "entity_ref",    // enum|quantity|date|struct|text|entity_ref
      "value_shape_spec": {           // shape-specific constraints for the editor
        "entity_type": "organization"
      },
      "drift_of": null,               // if this is a drift spelling, the canonical it maps to
      "registry_suggestions": [       // for the predicate picker
        {"id": "pred_employed_by", "canonical": "employed_by", "score": 0.97},
        {"id": "pred_works_with",  "canonical": "works_with",  "score": 0.55}
      ]
    },

    // value is a discriminated union keyed on predicate.value_shape.
    // For set-valued predicates this is an ARRAY of value cells, each independently editable.
    "values": [
      {
        "value_id": "val_88",         // stable per cell; targets array ops precisely
        "shape": "entity_ref",
        "entity_ref": {
          "entity_id": "ent_acme_01",
          "label": "Acme Corp",
          "candidates": [ /* same shape as subject.candidates */ ],
          "minted": false
        },
        "valid_from": {"instant": "2021-03-01", "precision": "month"},
        "valid_to":   null,           // null = ongoing
        "recurrence": null,           // rrule string when periodic
        "provenance": {
          "note_id": "note_8841", "span": [142, 167],
          "quote": "started at Acme in March 2021"
        },
        "confidence": 0.42,
        "head": true                  // which cell is "current head" for functional display
      }
    ],

    // examples of other value shapes (shown for one cell each):
    // "shape":"enum",     "enum": {"member":"married","domain":["single","married","divorced"]}
    // "shape":"quantity", "quantity": {"magnitude":72,"unit":"kg","unit_domain":"mass"}
    // "shape":"date",     "date": {"instant":"2026-07-04","precision":"day"}
    // "shape":"struct",   "struct": {"fields":{"street":"…","city":"…"}}
    // "shape":"text",     "text": {"value":"prefers window seat"}

    "reported_time": "2026-06-15T09:12:00Z",
    "source_note": {"note_id": "note_8841", "title": "call with Sam"}
  },

  "ui_capabilities": {                // server tells card what is allowed (Track F gates)
    "can_change_domain": true,
    "domain_move_requires_confirm": ["health->general", "finance->general"],
    "can_mint_entity": true,
    "can_split": true,
    "can_merge_with": ["fact_5570", "fact_5571"]
  }
}
```

Design notes:
- **`predicate.cardinality` is load-bearing.** It is the single signal that decides whether
  the value renders as one slot (functional → "replace") or an array (set → add/replace/remove).
  It comes from the predicate registry (PREDICATE_CANONICALIZATION), not inferred at the card.
- **`values[]` carries per-cell `valid_from/valid_to/recurrence/provenance/confidence`.** A
  set-valued fact is *one record with N temporally-scoped cells*, not N opaque rows. This
  lets "Sam worked at Acme 2021–2023, now at Beta" be one card with two cells, each with its
  own interval — and makes supersede-vs-add unambiguous (Track B owns the storage mapping).
- **`candidates[]` are precomputed** so relink is a pick, not a search round-trip. The picker
  still allows free search + mint.
- The payload is a **read projection**; the card never mutates it in place — it accumulates a
  **separate op list** (§2.2). This keeps the #7 doctrine clean: the displayed state is
  machine-authored; the human's contribution is a set of correction operations.

### 2.2 Edit submission (card → server)

A decision is **one verdict + an ordered list of typed ops** scoped to one or more facts.
Ops reference targets by stable ids (`fact_id`, `value_id`, `entity_id`) so they survive
concurrent reordering. This is Track C's algebra — Track E *consumes* it; the shapes below
are the consuming view, to be reconciled with C's canonical definitions.

```jsonc
{
  "schema_version": "edit-submission/1",
  "decision": {
    "item_id": "ri_01J…",
    "verdict": "approve_with_edits",  // approve | approve_with_edits | reject | defer | add
    "reason_code": "wrong_object_entity",  // required for reject/retract/supersede/domain move
    "note": "object was the wrong Acme; relinked",  // optional human note (audit only)
    "base_version": "fact_5567@v7",   // optimistic-concurrency token (RFC6902 `test` spirit)
    "client_ts": "2026-06-16T12:03:11Z"
  },

  "ops": [                            // ordered; applied atomically; empty for plain approve
    { "op": "relink_object",
      "fact_id": "fact_5567", "value_id": "val_88",
      "to_entity_id": "ent_acme_02" },

    { "op": "add_to_set",
      "fact_id": "fact_5567",
      "value": { "shape": "entity_ref", "entity_id": "ent_beta_01",
                 "valid_from": {"instant":"2023-09","precision":"month"} } },

    { "op": "set_valid_to",
      "fact_id": "fact_5567", "value_id": "val_88",
      "valid_to": {"instant":"2023-08","precision":"month"} },

    { "op": "set_modality",
      "fact_id": "fact_5567", "to": "asserted" },

    { "op": "pin", "fact_id": "fact_5567", "pinned": true }
  ],

  "structure_ops": [                  // span multiple facts; applied after field ops
    { "op": "split_fact",
      "from_fact_id": "fact_9001",
      "into": [
        { "subject_id":"ent_jeff", "predicate":"parent_of", "value":{"shape":"entity_ref","entity_id":"ent_summer"} },
        { "subject_id":"ent_jeff", "predicate":"parent_of", "value":{"shape":"entity_ref","entity_id":"ent_harmony"} },
        { "subject_id":"ent_jeff", "predicate":"parent_of", "value":{"shape":"entity_ref","entity_id":"ent_lydian"} }
      ] },
    { "op": "merge_facts", "fact_ids":["fact_12","fact_13"], "keep":"fact_12" }
  ],

  "identity_ops": [                   // entity-level; Track F firewall-gated
    { "op": "merge_entities", "entity_ids":["ent_sam_01","ent_sam_03"], "keep":"ent_sam_01" },
    { "op": "assert_distinct", "entity_ids":["ent_sam_01","ent_sam_07"] }
  ]
}
```

Design notes:
- **Verdict + ops are orthogonal.** `approve_with_edits` is the common path: the human keeps
  the fact but corrected three fields. `reject` carries a `reason_code` and (per doctrine)
  the server auto-drafts a correction note. `add` submits only `structure_ops`/new-fact ops.
- **`base_version` is the optimistic-concurrency guard** (the RFC-6902 `test` idea, named).
  If the underlying fact moved since the payload was rendered (e.g. nightly reprocess), the
  submission is rejected with a re-fetch, never silently clobbering.
- **Ordering matters and is explicit:** field ops → structure ops → identity ops, applied in
  one transaction. Track C owns conflict/commutativity rules; Track E only guarantees the
  card emits a deterministic order.
- **Why ops, not a corrected full record:** a full-record PUT loses *intent* (was the
  value-change a supersede or a typo fix?), can't carry reason codes per field, and is hard
  to reverse field-by-field. Ops give per-field audit, per-op reversibility, reason codes,
  and a natural #7 framing (human authored *operations*; machine authored *state*). RFC 6902
  JSON-Patch is the proven precedent; we use a **domain-typed** variant (relink/retime/split)
  rather than raw `path`-based patches so ops are meaningful to RLS, audit, and the registry.

---

## 3. Card information architecture

The card is a vertical stack of **field rows**, each `display ⇄ inline-editor`. Layout from
top to bottom (highest-signal first; rarely-touched fields collapse into a "more" drawer):

```
┌───────────────────────────────────────────────────────────── ReviewItem ───┐
│  ⚑ reason chip ("low confidence 0.42 · conflicts fact_5567")   [priority]   │
├─────────────────────────────────────────────────────────────────────────────┤
│  [Sam Rivera ▾]  — employed_by ▾ —  ▸ value area                            │  ← claim line
│   subject chip      predicate picker                                         │
├─────────────────────────────────────────────────────────────────────────────┤
│  VALUE AREA (shape- and cardinality-driven):                                │
│   functional → one slot:   [ Acme Corp ▾ ]  (replace)                       │
│   set-valued → array:                                                        │
│     • [Acme Corp ▾]  2021-03 → 2023-08   ⌫remove   ⇄replace                  │
│     • [Beta Inc ▾]   2023-09 → ongoing   ⌫remove                            │
│     [ + add value ]                                                          │
├─────────────────────────────────────────────────────────────────────────────┤
│  ⌄ temporal   ·   modality [asserted|negated|hypoth|reported|q|expected]    │
│  ⌄ domain (general)   ·   kind (relationship)   ·   confidence 0.42  📌pin   │
│  ⌄ provenance:  "started at Acme in March 2021"  → note "call with Sam"      │
├─────────────────────────────────────────────────────────────────────────────┤
│  ⟂ split   ⊕ merge…   ＋ add fact                                            │
├──────────────────────────────────────────── decision footer ───────────────┤
│  [Approve]   [Approve with edits (3)]   [Reject ▾ reason]   [Defer]          │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 3.1 Field → editor map

| Field | Editor widget | Notes |
|---|---|---|
| Subject | **entity chip → picker** | candidates ranked; free-search; cannot mint subject by default (subjects are usually known); shows disambiguator |
| Predicate | **combobox picker** | canonical list + registry suggestions; "map drift → canonical" and "coin new" actions inline; editing predicate may change `value_shape` → value editor re-renders |
| Qualifier | **chip on predicate** | small key:value (e.g. `audience:family`) |
| Value (functional) | **single typed slot** + "replace" | shape-specific: enum=segmented/select, quantity=number+unit-combobox, date=date+precision, struct=mini-form, text=textarea, entity_ref=entity picker |
| Value (set) | **array of value rows** | each row = typed value + its own temporal + remove; footer "+ add value"; one row marked head |
| Temporal | **interval sub-editor** | from/to with precision dropdown, "ongoing/ended/former" presets, rrule builder (defer detail to Track G), reported-time correction |
| Modality | **segmented control** | 6 states; non-asserted states tint the card |
| Domain | **select + consequence dialog** | health/finance/location ↔ general triggers a confirm explaining firewall impact (Track F) |
| Kind | **select** | changing kind re-keys which sub-editors show (e.g. measurement → forces quantity value + instant) |
| Confidence | **read + acknowledge/adjust** | |
| Pin | **toggle** | pinned facts survive reprocess |
| Provenance | **quote chip → re-anchor** | shows source span; re-anchor opens note with span selector |

### 3.2 Arrays (the override-vs-array core)

- The **predicate registry's `cardinality`** decides the rendering. Functional predicates
  (`date_of_birth`, `marital_status`) render **one slot**; the only mutation is *replace*
  (which becomes a `supersede` at storage, preserving history). Set-valued predicates
  (`employed_by`, `phone_number`, `parent_of`) render an **array** with explicit
  **add / replace-this-cell / remove-this-cell**, each cell independently temporally scoped.
- The UI **never silently replaces a head**: on a set-valued predicate there is no implicit
  override; "replace" is a per-cell action distinct from "add."
- If the human believes a *functional* predicate is wrong about its cardinality, they can
  flag it (`reclassify_cardinality` → registry self-improvement loop; deferred per
  PREDICATE_CANONICALIZATION). The card surfaces cardinality as a small label so the human
  understands why they see a slot vs. an array.

### 3.3 Split / merge / add-missing

- **Split:** a `⟂ split` control on any value array (or any fact) turns the current card into
  N child cards pre-seeded from the parent (subject/predicate carried down; the human edits
  each child's object/value). Emits one `split_fact` op with the children. The classic case
  ("my daughters Summer, Harmony, Lydian" → 3 `parent_of` edges) is a one-gesture split.
- **Merge:** a `⊕ merge…` control opens a **merge tray** listing `ui_capabilities.can_merge_with`
  candidates (same subject+predicate, compatible values); selecting them and a `keep` target
  emits `merge_facts`. Used to collapse duplicate extractions.
- **Add missing:** `＋ add fact` opens a **blank card** in the identical editor, subject
  pre-filled from context (the note's primary entity). The human fills predicate + value;
  emits `add_fact`. This is the "the extractor missed it" path through the *same* IA.
- **Identity ops** live on the **entity chip's detail popover** (split entity / merge entities
  / assert distinct-from), since they are entity-scoped, not fact-scoped, and are
  firewall-gated by Track F.

---

## 4. How the kind-zoo collapses

**Before:** N bespoke item types (fact-conflict, proposed-appointment, lab-row, wiki
split/merge, extraction-correction), each with its own card component, its own
accept/reject affordance, and ≤1 override.

**After:** **one card component** parameterized by `(kind, predicate.value_shape,
predicate.cardinality, reason)`. The "kind" is no longer a *card type*; it is a **field on
the fact** that conditionally swaps a sub-editor. The collapse mechanics:

1. **Item type → reason code.** What used to be a distinct item type ("proposed appointment")
   becomes a fact with `kind=event` surfaced for `reason=appointment_proposed`. Same card.
   The appointment publish action is a downstream consequence of approving an `event`-kind
   fact, not a separate review flow.
2. **Lab rows = measurement facts.** A lab result is a `kind=measurement`, `value_shape=quantity`
   fact in the `health` domain. The card already renders quantity+unit+reference-range as a
   struct/quantity editor. No bespoke lab card.
3. **Conflicts = a batch.** A fact-conflict is two+ facts sharing a `batch_key` with
   `reason=conflict`; the card renders them side-by-side and the decision resolves both
   (supersede one, merge, or keep-distinct). Not a separate item type.
4. **Wiki split/merge approvals** remain a review item but reuse the **split/merge structure
   ops** rather than a one-off approve button.
5. **Extraction corrections** are the default: any low-confidence/proposed fact is a card.

**Net effect (success criterion §5):** the count of bespoke "kinds" and one-off override
prompts goes *down* to ~1 card + a small set of sub-editors keyed off two enums (`kind`,
`value_shape`) and one boolean axis (`cardinality`). New predicates and new value shapes
extend the **value-editor registry**, not the card zoo.

---

## 5. Batching & audit

### 5.1 Batching

- **`batch_key`** groups items from one note or one conflict cluster. The inbox can present a
  **batch view**: all facts extracted from "call with Sam" on one screen, each an inline
  card, with a **batch decision footer** ("Approve all clean," "Approve all with my edits,"
  per-card overrides honored). This is the high-throughput path — the common case is "this
  note's 6 facts are all fine, one needs a relink."
- **Triage ordering** by `priority` + `reason.code`: conflicts and domain-move proposals
  float to the top; clean low-confidence facts batch at the bottom for bulk-approve.
- **Kanban-style queue** (best-practice from HITL review tools) optional: columns
  backlog / in-review / done, but the MVP is a single priority-ordered list with batch
  grouping. Drag is not required for correctness.
- One submission may carry ops for **multiple facts** (a batch decision = one
  edit-submission with ops across `fact_id`s, applied in one transaction).

### 5.2 Audit & reversibility (invariant §4)

- Every accepted submission is persisted as an **audit decision record**: the verdict, the
  reason code, the full op list, the `base_version`, the actor (device session), timestamp,
  and the resulting new fact versions. This is the unwind unit.
- **Reversibility = replay the inverse op list.** Because edits are typed ops with stable
  targets, each op has a defined inverse (Track C owns the inverse table:
  `add_to_set ⁻¹ = remove_from_set`, `supersede ⁻¹ = restore`, `relink ⁻¹ = relink-back`,
  `split ⁻¹ = merge`, etc.). "Reopen/undo" a decision replays inverses, restoring prior
  versions and reopening the review item. Track C must guarantee every op is invertible or
  explicitly mark it irreversible (e.g. `mint_entity` undo = soft-delete the orphaned entity).
- **Provenance preserved end-to-end:** every value cell keeps `note_id`+`span`; relink/retime
  ops do not erase provenance, they version it. A reject auto-drafts a correction note (per
  existing doctrine), linking the disputed fact — so even rejects are auditable as notes.
- **#7 doctrine reconciliation (the binding tension):** structured field edits are modeled as
  **machine-applied correction operations.** The human authors *operations*; the machine
  remains the sole author of *graph/wiki state*. The op log is the correction channel,
  generalizing today's free-text correction note into a typed, audited, reversible channel.
  **Position:** this preserves #7 without a doctrine change — the human still never writes
  graph state directly; they submit corrections the machine applies and can unwind. The
  free-text correction note is retained as a strict subset (the `note` field + auto-draft on
  reject) for cases an op can't express. *The red-team must attack whether "typed ops" are
  genuinely within #7's spirit or a back-door direct edit.*

---

## 6. Tradeoffs & risks

1. **Card complexity vs. the fewer-kinds win.** One parameterized card is harder to build than
   five simple ones, and risks an over-configurable "god component." Mitigation: the
   complexity is a **value-editor registry** keyed on `value_shape`, each editor small and
   independently testable; the card shell is thin.
2. **Cognitive load.** Exposing *every* field invites over-editing and slows triage.
   Mitigation: progressive disclosure — claim line + value always visible; temporal/domain/
   kind/provenance in collapsible drawers; batch "approve all clean" for the common case.
3. **`cardinality` correctness is a single point of failure.** If the registry mis-labels a
   predicate functional, the human silently can't add a second value. Mitigation: surface the
   cardinality label + a `reclassify_cardinality` escape hatch; default ambiguous predicates
   to *set* (additive is safe; silent-replace is the dangerous failure).
4. **Domain moves are a firewall hazard.** A relink or domain change could leak health→general.
   Mitigation: `ui_capabilities` gates from Track F; domain moves require an explicit confirm
   with consequence text and are always audited; entity relinks across firewall boundaries are
   blocked server-side regardless of UI.
5. **Optimistic concurrency vs. nightly reprocess.** A payload can go stale mid-review.
   Mitigation: `base_version` token; stale submissions re-fetch rather than clobber.
6. **Op-log audit storage growth.** Every decision stores a full op list + version pointers.
   Acceptable for a personal-scale system; flag for Track B's retention design.
7. **Asymmetric contracts (fat read / thin write) duplicate value-shape definitions.** Risk of
   drift between payload and submission value shapes. Mitigation: both derive from one shared
   value-shape schema (Track A) via codegen, versioned together (`schema_version`).

---

## 7. Open questions for the red-team

1. **#7 doctrine:** are typed correction operations truly within the machine-written doctrine,
   or a disguised direct edit? Is there a class of edit (e.g. coining a predicate, asserting
   distinct-from) that crosses the line and needs the explicit bounded doctrine change?
2. **Set-vs-functional default:** is "default ambiguous predicates to *set*" right, or does
   silent accumulation of near-duplicate values create worse graph pollution than silent
   replace?
3. **Per-cell temporal in a set-valued array:** is "one fact, N temporally-scoped cells" the
   right unit, or should each cell be its own fact (one-edge-per-value)? This is the Track A/B
   boundary; the card IA assumes cells but can render either — which serializes more cleanly
   for split/merge and supersession?
4. **Batch atomicity:** if one op in a multi-fact batch submission fails RLS/validation, does
   the whole batch roll back (safer, but loses the 5 clean approvals) or partially commit
   (faster, but harder to audit)? Track C + F input needed.
5. **Conflict resolution UX:** is side-by-side N-fact comparison enough, or do conflicting
   temporals/modalities need a dedicated reconciliation editor beyond the standard card?
6. **Optimistic-concurrency UX:** how disruptive is a forced re-fetch mid-edit if nightly
   reprocess churns facts during a long review session? Should review pin facts on open?
7. **Mint-entity abuse surface:** relink-to-new-entity lets the human mint entities from the
   review card. What stops accidental entity proliferation, and how does Track F gate minting
   inside firewalled domains?
8. **Predicate coining from the card:** does letting a human coin a predicate mid-review
   bypass the canonicalization registry's quality controls, and should coined predicates land
   in a "provisional" state pending the registry self-improvement loop?

---

*Consumes:* Track C (operation algebra, inverses, conflict rules), Track A (value-shape
schema), Track B (storage mapping of cells/cardinality/supersession), Track G (temporal
sub-editor), Track F (firewall gates on domain moves, relinks, minting).

### Sources (external best practice)
- [Human-in-the-Loop Review Workflows for LLM Applications & Agents — Comet](https://www.comet.com/site/blog/human-in-the-loop/)
- [How to improve your golden datasets with human review — Braintrust](https://www.braintrust.dev/blog/human-review-golden-datasets)
- [RFC 6902: JSON Patch — RFC Editor](https://www.rfc-editor.org/rfc/rfc6902.html)
- [Entity resolution and knowledge graphs — Linkurious](https://linkurious.com/blog/entity-resolution-knowledge-graph/)
- [Entity-resolved knowledge graphs: a tutorial — Neo4j](https://neo4j.com/blog/developer/entity-resolved-knowledge-graphs/)
