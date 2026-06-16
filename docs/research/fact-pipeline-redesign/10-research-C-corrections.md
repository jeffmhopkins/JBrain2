# Track C — Correction taxonomy, "array vs. override," and the #7 doctrine

**Status:** Phase-1 research brief (greenfield, first-principles). Input to synthesis (`20-spec-v0.md`).
**Scope:** the complete algebra of human correction *operations* over a fact and the fact set;
functional-vs-set semantics; reconciliation with invariant §4 ("machine-written wiki," rule #7);
per-operation graph mutation + audit record + undo.
**Reads:** binding brief `00-framing.md` (§2 wishlist, §3 contract, §4 invariants, §5 success).
Skimmed for invariant context only: `ARCHITECTURE.md` (facts/wiki/correction loop), `PREDICATE_CANONICALIZATION.md`.
Did **not** read `backend/src`.

---

## 1. Proposal

### 1.1 Thesis in one paragraph

Treat every human correction as a **typed, named, intent-bearing operation** appended to a
per-fact **correction op-log**, not as a free-form diff or a corrected full record. A fixed,
closed set of ~22 operation types (the *algebra*) covers every §2 wishlist item. Each operation
is a pure function `op : GraphState → (GraphState, AuditRecord, InverseOp)`: applying it mutates
the graph, emits one immutable audit row, and yields a precomputed **inverse operation** that is
itself a member of the algebra. The op-log is the source of truth for *why the graph looks the way
it does post-correction*; the graph is a materialized projection. This is event sourcing
([Fowler](https://martinfowler.com/eaaDev/EventSourcing.html),
[Azure](https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing)) specialized
to the fact domain, with **compensating (inverse) operations** for undo rather than raw state
rollback.

### 1.2 Why typed intent operations, not JSON Patch or "corrected record"

Three candidate representations of an edit (the §3 tension):

1. **Corrected full record** — human submits the whole fact as they want it. Loses *intent*
   (was the predicate change a remerge or a reclassification?), can't express set-add vs.
   replace (both look like "the value field changed"), and can't express split/merge/identity ops
   at all. Reject.
2. **Generic JSON Patch (RFC 6902** — `add`/`remove`/`replace`/`move`/`copy`/`test`). Mechanically
   sound and well-understood, and RFC 6902 patches *can* be made invertible by preceding each
   `remove`/`replace` with a `test` that captures the old value
   ([rfc6902](https://www.rfc-editor.org/rfc/rfc6902.html), [jsonpatch.com](https://jsonpatch.com/)).
   But a path-and-value patch is **semantically blind**: `replace /value` and `add /-/value` are
   the *same machinery* whether the predicate is functional or set-valued, so the "override vs.
   array" ambiguity the brief calls the core problem is *reintroduced at the representation layer*.
   It also cannot carry firewall consequences, supersession semantics, or entity-identity intent.
   Reject as the *primary* contract; **reuse its discipline** (each destructive op captures the
   prior value to be invertible) inside our typed ops.
3. **Typed intent operations** (chosen). Each op names *what the human meant* (`add_to_set`,
   `replace_head`, `relink_object`, `split_fact`, `merge_entities`…). The functional-vs-set choice
   is encoded in *which operation the human invokes*, surfaced by the UI from the predicate's
   registry `functional` flag — so override-vs-add is structurally unambiguous, not a guess over a
   path. Intent ops are also the natural audit and undo unit (one op = one audit row = one inverse).

The cost of (3) is a closed vocabulary that must be maintained and versioned. We accept that: a
*closed* algebra is exactly what makes "fewer bespoke review kinds" (success §5) achievable — the
review UI renders N operation editors, not N one-off prompts.

### 1.3 Layering

```
human action in review UI
   └─ emits a CorrectionOp (typed, validated against op schema vK)
        └─ arbiter validates (firewall, identity, shape, op-preconditions)  ← deterministic gate
             ├─ reject → op recorded as rejected, no graph mutation
             └─ accept → 1 graph mutation  +  1 audit row  +  1 stored inverse op
                            └─ (optional) drafts a correction note for the wiki loop (see §4)
```

The **arbiter** (already the deterministic committer in `ARCHITECTURE.md`) is the only writer.
Humans never write the graph directly; they *propose operations* the arbiter applies — this is the
hinge of the #7 reconciliation (§4).

---

## 2. The operation algebra (concrete schema + JSON)

### 2.1 The fact shape these ops operate on (assumed, owned by Tracks A/B)

This brief assumes a fact/edge with at least:

```jsonc
{
  "fact_id": "f_0001",
  "subject_entity_id": "e_sam",
  "predicate": "person.employer",            // canonical (Track: predicate canonicalization)
  "qualifier": null,                          // e.g. nickname audience
  "value": { "shape": "ref", "object_entity_id": "e_acme" },  // typed per predicate value_shape
  "modality": "asserted",                     // asserted|negated|hypothetical|reported|question|expected
  "domain": "general",                        // general|health|finance|location
  "kind": "relationship",                     // event|measurement|state|attribute|preference|relationship
  "valid_from": "2021-03-01", "valid_to": null,
  "valid_precision": "month",                 // instant|day|month|year|era|unknown
  "rrule": null,
  "reported_at": "2026-06-15T10:00:00Z",      // bitemporal: when captured (distinct from valid-time)
  "provenance": { "note_id": "n_42", "span": [120, 168] },
  "confidence": 0.82,
  "pinned": false,
  "set_member_id": "m_01",                    // identity of THIS value within a set-valued predicate
  "superseded_by": null,
  "status": "live"                            // live|superseded|retracted
}
```

The `set_member_id` is load-bearing: every value of a set-valued predicate is an addressable
member, so add/replace-head/remove target a member, never a positional index (positional JSON-Patch
indices are the classic source of the "silently replaced the head" bug).

### 2.2 Common envelope

Every operation shares one envelope; the `op` field selects the variant and its `args`:

```jsonc
{
  "op_id": "op_7f3a",                  // ulid, monotonic → total order of the op-log
  "op": "add_to_set",                  // the operation type (closed enum, §2.3)
  "schema_version": 3,                 // op-contract version; migratable (§5 success)
  "target": { "fact_id": "f_0001" },   // or {entity_id}, or {fact_ids:[...]} for merge
  "args": { /* per-op, below */ },
  "actor": { "kind": "human", "id": "owner" },   // human | agent (ASSISTANT.md) | system
  "reason": "free text, optional",     // human's note; NOT the edit itself
  "preconditions": { /* captured prior state for invertibility (the RFC-6902 `test` discipline) */ },
  "client_ts": "2026-06-16T14:00:00Z"
}
```

`preconditions` is the invertibility hook from RFC 6902: a destructive op records the value it is
about to overwrite/remove, so its inverse is fully determined and the undo never has to "guess"
([rfc6902 §invertibility](https://www.rfc-editor.org/rfc/rfc6902.html)).

### 2.3 The closed operation set

Grouped by §2 wishlist coverage. For each: **mutation**, **audit**, **undo**. Audit fields beyond
the envelope are noted; every op writes one `correction_audit` row (§2.5).

#### Group A — per-field set ops on one fact (wishlist 1,2,5,6,7,8)

A single generic op `set_field` with a typed `field` discriminator covers predicate / qualifier /
modality / domain / kind / a *functional* value, plus `retime` for the temporal bundle. This is the
biggest "fewer kinds" lever: one op, one editor, field-typed.

**`set_field`** — args `{ field, new_value, ... }` where `field ∈
{predicate, qualifier, value, modality, domain, kind, confidence, valid_precision, rrule}`.

| field | mutation | undo |
|---|---|---|
| `predicate` | rewrite `predicate` to a canonical (or trigger mint via the predicate-canon path); if old/new differ in `functional`, **re-key** the fact (a functional→set or set→functional move is validated, may demand a member split — arbiter rejects if ambiguous) | `set_field predicate = old` |
| `value` (functional only) | replace the literal; coerce/validate against the predicate `value_shape`; old value goes to `preconditions` | `set_field value = old` |
| `modality` | overwrite | inverse `set_field` |
| `domain` | **firewall move** — arbiter re-evaluates RLS scope; consequences surfaced *before* commit (§6); requires the destination domain be writable by the actor | inverse `set_field` (re-asserts old domain; must also be RLS-legal) |
| `kind` | reclassify | inverse |
| `qualifier` | set/clear | inverse |

`set_field value` on a **set-valued** predicate is a *hard error* — the UI must never offer it; it
offers `add_to_set` / `replace_head` / `remove_from_set` instead (§3). This is the structural
guarantee against override-vs-add ambiguity.

```jsonc
// "this was hypothetical, not asserted"
{ "op": "set_field", "target": {"fact_id":"f_9"},
  "args": { "field": "modality", "new_value": "hypothetical" },
  "preconditions": { "modality": "asserted" } }
```

**`retime`** — args `{ valid_from?, valid_to?, valid_precision?, reported_at?, rrule? }`. One op for
the whole temporal bundle (so "mark former" = set `valid_to`; "ongoing" = clear it). Bitemporal
rule: `reported_at` corrections write a *new* reported-time but never erase the original capture row
— the audit log preserves both (invariant §4 bitemporal).

```jsonc
{ "op": "retime", "target": {"fact_id":"f_emp"},
  "args": { "valid_to": "2024-12-31", "valid_precision": "day" },
  "preconditions": { "valid_to": null, "valid_precision": "month" } }
// mutation: close the interval. audit: temporal delta. undo: retime back to (null, month).
```

#### Group B — entity-link ops (wishlist 3,4)

**`relink_subject`** — args `{ new_subject_entity_id }`. Mutation: repoint `subject_entity_id`;
**arbiter validates the target entity is firewall-legal for the fact's domain** (no cross-firewall
leak — invariant §4). Undo: `relink_subject` to old id.

**`relink_object`** — args `{ new_object_entity_id }` (existing entity). Same validation. Undo:
relink to old.

**`mint_and_link_object`** — args `{ new_entity: {kind, name, attrs} }`. Mutation: mint entity +
relink object to it. Audit records the minted entity id. **Undo is compound**: `relink_object` back
to old + `retract_entity` of the freshly minted one (only safe to delete because the audit proves it
was minted by *this* op and is referenced nowhere else — arbiter checks ref-count = 1).

**`unlink_object`** — args `{}`. Mutation: convert a relationship edge to a dangling/value-less
state or retract per predicate rules. Undo: relink to old object.

```jsonc
{ "op": "relink_subject", "target": {"fact_id":"f_12"},
  "args": { "new_subject_entity_id": "e_sam_carter" },
  "preconditions": { "subject_entity_id": "e_sam_jones" } }
```

#### Group C — cardinality / array ops (wishlist 9 — the core)

These exist **only** for set-valued predicates; for functional predicates the UI doesn't render
them (§3).

**`add_to_set`** — args `{ value }`. Mutation: insert a new member (`set_member_id` minted) under
`(subject, predicate)`. Audit: the minted member id. Undo: `remove_from_set` of that member.

**`remove_from_set`** — args `{ set_member_id }`. Mutation: mark that member retracted (kept for
citation integrity). `preconditions` snapshots the member's full value. Undo: `add_to_set` re-adding
the snapshotted value (re-using the same `set_member_id` so history threads).

**`replace_head`** — args `{ set_member_id, new_value }`. Sugar for "supersede *this* member with a
new value, keep history." Mutation: supersede the member, add a successor member linked via
`superseded_by`. Undo: reverse the supersession (drop successor, un-supersede predecessor). This is
the operation that fixes "single-value override silently replaced the head" — it is *explicitly*
distinct from `add_to_set`, and it targets a named member, not "the head."

```jsonc
// add a second employer — NOT a replace
{ "op": "add_to_set", "target": {"fact_id":"f_emp_sam"},   // f_emp_sam = the (Sam, employer) group
  "args": { "value": { "shape":"ref", "object_entity_id":"e_globex" } } }

// correct ONE phone number, keeping the old in history
{ "op": "replace_head", "target": {"fact_id":"f_phone_sam"},
  "args": { "set_member_id":"m_03", "new_value": {"shape":"text","text":"+1-555-0100"} },
  "preconditions": { "value": {"shape":"text","text":"+1-555-9999"} } }
```

#### Group D — structure ops (wishlist 10,11)

**`split_fact`** — args `{ parts: [ {subject?, predicate?, value, qualifier?, kind?} , ... ] }`.
Mutation: retract the parent fact (status `superseded`, `split_into` pointer), mint one child fact
per part, copying inherited provenance/time/domain unless overridden. Audit: parent→children map.
Undo: `merge_facts` of the children back into the parent (restore parent to `live`, retract
children). The canonical "my daughters Summer, Harmony, Lydian → three edges" case.

```jsonc
{ "op": "split_fact", "target": {"fact_id":"f_kids"},
  "args": { "parts": [
     { "predicate":"person.child", "value":{"shape":"ref","object_entity_id":"e_summer"} },
     { "predicate":"person.child", "value":{"shape":"ref","object_entity_id":"e_harmony"} },
     { "predicate":"person.child", "value":{"shape":"ref","object_entity_id":"e_lydian"} } ] } }
```

**`merge_facts`** — args `{ fact_ids: [...], into: {predicate?, value?, ...} }`. Mutation: retract
the inputs, mint one merged fact, union provenance spans (all sources cited). Audit: inputs→merged
map. Undo: `split_fact` restoring the originals from the audit snapshot.

**`add_fact`** — args `{ subject, predicate, value, ... , provenance }`. The full structured editor
producing a fact the extractor never emitted. Provenance is **mandatory** (must cite a note/span —
invariant §4); a human-authored fact with no source is rejected by the arbiter, *or* is admitted
with `provenance.kind = "human_assertion"` pointing at the correction op itself as the source — a
bounded, audited exception (see §6 risk). Undo: `retract` the fact.

#### Group E — lifecycle / trust ops (wishlist 13,14,15)

**`retract`** — args `{ reason_class: misread|wrong|duplicate }`. Mutation: status→`retracted`,
stays queryable for citation integrity (newest-wins arbiter ignores it). Undo: `unretract` (restore
`live`). Distinct from drop-at-review (which never committed).

**`supersede`** — args `{ new_value | new_fact }`. Mutation: status→`superseded`, link
`superseded_by` to the successor (for *functional* predicates; the set analog is `replace_head`).
Undo: drop successor, un-supersede.

**`pin`** / **`unpin`** — args `{}`. Mutation: toggle `pinned`; pinned facts survive reprocessing
(the integrator may not drop them). Undo: the opposite toggle.

**`set_confidence`** — args `{ new_confidence }` or `{ acknowledge: true }`. Mutation: overwrite/clamp
confidence; `acknowledge` records human review without changing the value. Undo: restore old.

**`fix_provenance`** — args `{ note_id, span }`. Mutation: repoint the cited source span. Undo:
restore old provenance. (Cannot fabricate provenance — the cited note must exist and be readable by
the actor's domain scope.)

#### Group F — entity-identity ops (wishlist 12)

These operate on the **entity** target, not a single fact, and ripple to every fact referencing the
entity. Reversibility here is the hard part; we use the MDM **non-destructive merge** pattern
(keep source records, store a merge *link* rather than physically collapsing — unmerge is then just
deleting the link)
([CluedIn](https://www.cluedin.com/agentic-data-management-platform),
[Profisee](https://www.profisee.com/Platform/Golden-Record-Management)).

**`merge_entities`** — args `{ source_entity_ids: [a,b], survivor: a, survivorship: {...} }`.
Mutation: create a `survivor` golden entity referencing `a` and `b` as merged sources via a
`merge_link` (the sources are *retained*, marked `merged_into = survivor`); facts pointing at `a`/`b`
now resolve through the link to `survivor`. Survivorship rules pick attribute winners and are
recorded per-attribute in the audit (attribute-level survivorship audit, the MDM standard). Undo:
`unmerge_entities` — delete the merge_link, re-expose sources; facts re-resolve to their original
source ids (which is why we never rewrote the facts' stored `*_entity_id` — only resolution
changed). **Firewall rule:** an entity that participates in `health`/`finance`/`location` facts may
only merge with an entity of compatible scope; the arbiter rejects a merge that would let a
general-domain reader resolve to a health-scoped golden entity (invariant §4, no cross-firewall
leak).

**`split_entity`** — args `{ entity_id, into: [{...},{...}], assignment: {fact_id → new_entity_id} }`.
The inverse-in-spirit of merge for an over-merged entity ("two different Sams collapsed to one").
Mutation: mint the new entities; reassign each fact per `assignment`. Undo: `merge_entities` of the
splits back, using the recorded assignment to restore. Requires an explicit per-fact assignment
(no heuristic) so it is deterministic and reversible.

**`assert_distinct`** — args `{ entity_id_a, entity_id_b }`. Mutation: write a `distinct_from`
constraint that blocks future auto-merge of the pair (a negative training signal). Undo: delete the
constraint. Cheap, non-destructive, high-value (stops the same bad merge recurring).

### 2.4 Operation → graph-mutation / audit / undo summary table

| op | wishlist | graph mutation | undo op |
|---|---|---|---|
| `set_field` (predicate/qualifier/value*/modality/domain/kind/confidence) | 1,2,6,7,8 | rewrite field; re-key/firewall-revalidate as needed | `set_field` to prior |
| `retime` | 5 | edit valid-interval / precision / rrule / reported_at (append) | `retime` to prior |
| `relink_subject` / `relink_object` | 3,4 | repoint entity id (firewall-checked) | relink to prior |
| `mint_and_link_object` | 4 | mint entity + relink | relink-prior + retract-minted (ref-count 1) |
| `unlink_object` | 4 | drop object link | relink to prior |
| `add_to_set` | 9 | insert member | `remove_from_set` |
| `remove_from_set` | 9 | retract member (kept) | `add_to_set` (same member_id) |
| `replace_head` | 9 | supersede member + successor | reverse supersession |
| `split_fact` | 10 | retract parent + mint children | `merge_facts` |
| `merge_facts` | 10 | retract inputs + mint merged | `split_fact` |
| `add_fact` | 11 | mint fact (provenance required) | `retract` |
| `retract` / `unretract` | 13 | status toggle (queryable) | the opposite |
| `supersede` | 13 | status + `superseded_by` | drop successor + un-supersede |
| `pin` / `unpin` | 14 | toggle `pinned` | opposite |
| `set_confidence` | 14 | overwrite/ack confidence | restore |
| `fix_provenance` | 15 | repoint span | restore |
| `merge_entities` / `unmerge_entities` | 12 | merge_link (non-destructive) | the opposite |
| `split_entity` | 12 | mint + reassign | `merge_entities` |
| `assert_distinct` | 12 | distinct_from constraint | delete constraint |

\* `set_field value` is **functional-predicate only** (§3).

### 2.5 The audit record (one immutable row per applied op)

```jsonc
{
  "audit_id": "au_9920",
  "op_id": "op_7f3a",                 // the operation that caused it
  "op": "add_to_set",
  "schema_version": 3,
  "actor": { "kind": "human", "id": "owner" },
  "target_before": { /* snapshot of affected fact(s)/entity sufficient to reconstruct */ },
  "target_after":  { /* snapshot after */ },
  "inverse_op": { /* a fully-formed CorrectionOp that undoes this; precomputed at apply time */ },
  "graph_writes": [ { "table": "facts", "row_id": "f_…", "before": {...}, "after": {...} } ],
  "correction_note_id": "n_77",       // if this op drafted a wiki correction note (§4); else null
  "applied_at": "2026-06-16T14:00:01Z",
  "undone_by": null                   // set when a later undo consumes this (audit stays; never deleted)
}
```

The audit table is **append-only** and never mutated except to stamp `undone_by`. Undo does not
delete the original audit row; it appends a *new* op (the stored `inverse_op`) which itself writes
its own audit row. Replaying the op-log from genesis reconstructs the graph (event-sourcing
guarantee), so audit + reversibility (invariant §4) are structural, not bolted on.

---

## 3. Functional-vs-set rule (override-vs-add made unambiguous)

### 3.1 The rule

**Every predicate carries a registry `functional` flag** (already declared in
`schema/defs/**.yaml` and surfaced by `PREDICATE_CANONICALIZATION.md`; the hardcoded
`FUNCTIONAL_PREDICATES` set folds into the `canonical_predicates.functional` column). The flag
**deterministically selects which operations are legal** on a fact's value:

| predicate `functional` | legal value-ops | illegal (UI must not offer) |
|---|---|---|
| `true` (single-valued: `birthDate`, `name.full`) | `set_field value`, `supersede` | `add_to_set`, `replace_head`, `remove_from_set` |
| `false` (set-valued: `employer`, `child`, `phone`) | `add_to_set`, `replace_head`, `remove_from_set` | `set_field value` (hard error) |

This is the entire fix for the brief's core problem. Override-vs-add is **never a free choice over a
generic path**; it is *determined by data* (the predicate's cardinality) and *enforced by the
arbiter*. A human literally cannot issue `add_to_set` on `birthDate` or `set_field value` on
`employer` — those ops fail precondition validation.

### 3.2 How the human is shown which applies (the surfacing contract)

The review payload for a fact carries a `cardinality` block the UI binds to directly (Track E owns
the visuals; this is the contract):

```jsonc
"cardinality": {
  "functional": false,
  "label": "set-valued",                 // human words: "can have several"
  "current_members": [
    {"set_member_id":"m_01","value":{...},"superseded":false},
    {"set_member_id":"m_02","value":{...},"superseded":false}
  ],
  "offered_ops": ["add_to_set","replace_head","remove_from_set"]   // arbiter-authoritative
}
```

- For a **functional** predicate the editor shows *one* value control with a single "Change value"
  action (→ `set_field value`) and a "Supersede (keep history)" action. No "add another."
- For a **set-valued** predicate the editor shows the member list, an "**Add another**" button
  (→ `add_to_set`), and per-member "Correct this / Remove this" actions (→ `replace_head` /
  `remove_from_set`). There is no bare "Change value."

`offered_ops` is computed by the arbiter from the registry, never by the client — so even a
mis-rendered or hostile client cannot smuggle a `set_field value` onto a set predicate.

### 3.3 Edge: changing a predicate's cardinality

If a `set_field predicate` op moves a fact onto a predicate with a *different* `functional` flag,
the arbiter requires the value to be reconciled in the *same* op (e.g. functional→set wraps the lone
value as the first member; set→functional with >1 live member is **rejected** with "remove or merge
members first"). This keeps the cardinality invariant total — there is no transient ambiguous state.

---

## 4. The #7 doctrine position (the critical call)

### 4.1 Position

**Structured field edits CAN be modeled as machine-applied, audited, reversible correction
operations that PRESERVE the #7 doctrine. No doctrine change is required — only a precise
*reading* of what #7 forbids, plus one small, explicitly-bounded clarification.**

### 4.2 The rule (verbatim, binding)

> **#7 forbids humans *authoring graph/wiki state by hand*. It does not forbid humans *issuing
> typed correction operations that the deterministic arbiter validates and applies*.** A correction
> operation is a *machine-applied* mutation: the human supplies *intent and arguments*; the arbiter
> (machine) decides legality, performs the write, and emits the audit + inverse. Therefore every
> operation in §2 is doctrine-compliant **iff** all four hold:
>
> 1. **No direct write.** The human never mutates a fact/entity/article row; the arbiter does.
> 2. **Closed, typed vocabulary.** The human picks from the §2 algebra; no free-form state injection.
> 3. **Audited + reversible.** Every applied op writes an immutable audit row carrying a
>    fully-formed inverse op (§2.5); nothing is unwindable-only-in-theory.
> 4. **Wiki stays machine-written.** Corrections touch the *fact graph*; the **wiki is regenerated
>    from facts by the machine** on the next pass (`ARCHITECTURE.md` incremental build). A
>    correction op MAY draft a correction note (the existing channel) to nudge the wiki, but it
>    **never edits article prose**.

### 4.3 Why this is the right reading, not a loophole

The doctrine's *purpose* (`ARCHITECTURE.md`: "the owner never edits articles… a correction note…
flows through normal ingestion") is **provenance and regenerability**: the wiki must remain a pure
function of the fact graph so it can be rebuilt, cited, and trusted. Field corrections on *facts*
don't violate that — they change the *inputs* to the function, through an audited channel, and the
wiki re-derives. The thing #7 actually guards against is *un-provenanced, un-regenerable human prose
leaking into machine-authored output*. Typed correction ops are the opposite: maximally provenanced
(op_id + audit + inverse), and the wiki still regenerates from facts. So richer review is **inside**
the doctrine, not a relaxation of it.

Contrast: the *naive* richer-review design — "let the human edit the fact JSON directly" — *would*
violate #7 (direct human authorship of graph state, no intent, weak undo). The typed-op framing is
exactly what keeps us compliant while delivering §2.

### 4.4 The one bounded clarification (the honest part)

`add_fact` (wishlist 11) lets a human introduce a fact the *extractor never produced*. Strictly,
that is human-*originated* graph content, which brushes against #7's spirit even though it goes
through the arbiter. **Bounded rule:** a human-originated fact is admitted only with
`provenance.kind = "human_assertion"`, citing the correction op as its source, and is **flagged in
the audit and visibly attributed** wherever it surfaces. It is *not* free prose in the wiki; it is a
typed, provenanced fact like any other, and the wiki cites it as human-asserted. This is the single
explicit, narrowly-scoped extension of the doctrine; the red-team should attack whether it should
instead be forced through a correction *note* (round-tripping the extractor) rather than direct
`add_fact`. We lean toward `add_fact` with mandatory human-assertion provenance because forcing a
note for "the model literally missed my daughter's name" is poor ergonomics (success §5: cognitive
load down) and the provenance flag preserves auditability — but we flag it as the doctrine's only
soft edge.

---

## 5. Tradeoffs & alternatives

- **Closed algebra vs. open patch language.** We chose closed (22 ops). Tradeoff: every new
  correction need is a *schema change* (versioned, migrated — §2.2 `schema_version`). Benefit:
  unambiguous intent, fewer review kinds, structural functional/set safety. A generic patch language
  would be infinitely flexible and *exactly* reintroduce the override-vs-array ambiguity. Accepted.
- **`set_field` super-op vs. one op per field.** We merged predicate/qualifier/modality/domain/kind/
  confidence into `set_field` with a `field` discriminator (fewer kinds), but kept `retime`,
  `relink_*`, and the cardinality ops separate because they carry distinct preconditions/firewall
  logic. A purist "one op per field" is more uniform but explodes the kind count; a single
  "edit_fact" mega-op loses intent. The split is the pragmatic middle.
- **Non-destructive entity merge vs. physical collapse.** Non-destructive (merge_link) makes
  `unmerge` trivial and firewall-auditable, at the cost of a resolution indirection on every entity
  read and a retained-sources storage overhead. Physical collapse is faster to read but makes
  unmerge a forensic reconstruction. We chose reversibility (invariant §4) over read speed; if the
  indirection costs too much, cache the resolution, don't drop the link.
- **Inverse-op-per-audit-row vs. recompute-undo-on-demand.** We precompute and store the inverse at
  apply time (RFC-6902 invertibility discipline). Costs a little storage; buys deterministic,
  side-effect-free undo even after later ops, and a human-readable "what will undo do" preview.
- **Op-log as source of truth vs. graph as source of truth.** Full event-sourcing (replay from
  genesis) is the clean ideal but heavy. Pragmatic middle (recommended): the **graph is the live
  store**, the op-log + audit is the **authoritative change history**; we don't *require* replay for
  normal operation, only for forensic reconstruction and undo. This keeps reads cheap while
  preserving the audit/undo invariant.

---

## 6. Risks & failure modes

1. **Firewall leak via relink/merge/domain-move.** The richest new surface. A `relink_subject` to a
   health-scoped entity, a `merge_entities` across firewalls, or a `set_field domain` health→general
   could *exfiltrate* protected facts. **Mitigation:** the arbiter re-runs full RLS/firewall
   validation on *every* link/merge/domain op against the *actor's* scope and the *fact's* domain;
   cross-firewall ops are rejected by default and require an explicit, separately-audited
   domain-move op that shows the consequence ("this fact becomes visible to general-domain readers")
   *before* commit. This overlaps Track F — flag for joint review. Sev-1 if missed.
2. **Domain-move undo re-leaks.** Undoing a health→general move must restore the health scope, but if
   the fact was read/cited while general, the cite may now dangle across a firewall. **Mitigation:**
   domain-move audit records downstream cites; undo flags them for re-evaluation rather than silently
   breaking. Open question for red-team.
3. **`add_fact` doctrine erosion.** §4.4. If human-asserted facts proliferate, the "machine-written"
   claim weakens. **Mitigation:** hard provenance flag + visible attribution + a metric (count of
   human-asserted facts) that the red-team/owner can watch.
4. **Set-member identity drift across reprocessing.** If the extractor re-runs and re-mints members,
   a prior `remove_from_set` keyed on a stale `set_member_id` could resurrect a removed value.
   **Mitigation:** members carry stable identity tied to (subject, predicate, value-hash); pinned and
   human-touched members are protected from reprocessing churn (the `pin` op exists for exactly this).
5. **Merge/split non-determinism.** `merge_facts` provenance-union or `split_entity` assignment that
   isn't fully specified could be irreversible. **Mitigation:** both ops require *explicit* maps
   (parts list, per-fact assignment) — no heuristic in the op; the arbiter rejects underspecified
   structure ops.
6. **Schema-version skew.** A v2 inverse op stored in an audit row, replayed under a v4 op processor.
   **Mitigation:** `schema_version` on every op; op-processor keeps a migration ladder; inverses are
   re-validated, not blindly applied, on undo.
7. **Concurrent ops / lost update.** Two ops target the same fact between read and apply.
   **Mitigation:** `preconditions` act as optimistic-concurrency `test`s (RFC-6902 discipline); a
   precondition mismatch rejects the op and re-opens the review item with fresh state.
8. **Hostile/buggy client smuggling an illegal op** (e.g. `set_field value` on a set predicate).
   **Mitigation:** `offered_ops` and all cardinality/firewall checks are *arbiter-side*; the client
   is never trusted. Echoes invariant "deterministic arbiter commits."

---

## 7. Open questions for the red-team

1. **`add_fact` (§4.4):** direct typed op with human-assertion provenance, or force every
   human-originated fact through a correction *note* that round-trips the extractor? Which better
   preserves #7 without wrecking ergonomics?
2. **Domain-move reversibility (risk 2):** is undo of a firewall crossing ever *truly* safe once the
   fact has been read/cited across the boundary, or must domain-move be one-way-with-new-fact rather
   than a reversible `set_field`?
3. **Cardinality flips (§3.3):** is rejecting set→functional with >1 member the right call, or should
   the op auto-offer a `merge_facts` to reconcile? Auto-offer is friendlier but couples two ops.
4. **Op-log vs. graph as truth (§5):** do we ever *need* full replay-from-genesis, or is "graph
   live + op-log authoritative history" sufficient for the audit/undo invariant? If we never replay,
   are stored inverses enough, and can they drift from a graph mutated by a non-op path (reprocessing)?
5. **Entity merge resolution cost (risk, §5):** does non-destructive merge_link indirection blow the
   per-read budget at scale, and if cached, how is the cache invalidated on `unmerge` without a
   firewall race?
6. **Granularity of `set_field`:** is the field-discriminated super-op too clever — does collapsing
   domain (firewall) and confidence (trivial) into one op-type hide risk that per-op types would make
   explicit? (Over-engineering lens vs. fewer-kinds lens — directly in tension.)
7. **Member identity (risk 4):** is value-hash member identity stable enough, or do humans need to
   *name* members for `remove_from_set`/`replace_head` to be safe across reprocessing?
8. **Undo of structure + identity ops in combination:** does undoing a `split_fact` whose children
   were later `relink_object`'d compose correctly, or do cross-op dependencies need an explicit
   dependency graph (undo-blocked-by)?

---

### Sources
- Event sourcing / compensating events: [Fowler](https://martinfowler.com/eaaDev/EventSourcing.html), [Azure Architecture Center](https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing)
- Invertible patches / `test` discipline: [RFC 6902](https://www.rfc-editor.org/rfc/rfc6902.html), [jsonpatch.com](https://jsonpatch.com/)
- Provenance modeling (named graphs over reification): [metaphacts](https://blog.metaphacts.com/citation-needed-provenance-with-rdf-star), [Springer survey](https://link.springer.com/article/10.1007/s41019-020-00118-0)
- Non-destructive merge / attribute-level survivorship audit: [CluedIn](https://www.cluedin.com/agentic-data-management-platform), [Profisee golden-record](https://www.profisee.com/Platform/Golden-Record-Management)
