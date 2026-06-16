# Track A — Fact intermediate representation (in-flight fact shape)

**Status:** Phase-1 research brief (fan-out, "deep"). Greenfield, first-principles.
**Scope owned:** the in-flight *logical* shape(s) of a single fact from
sentence → extraction-output → integration-input; the typing of values; the
representation of entity links pre- vs post-resolution; how multi-valued
properties carry "add vs replace" intent. **Out of lane:** persistence/bitemporal
intervals (Track B), time-depth/recurrence semantics (Track G), firewall
enforcement mechanics (Track F), the human edit-operation algebra (Track C),
prompt elicitation (Track D), review UX (Track E). I touch their surfaces only at
the contract boundary.

---

## 1. Proposal (headline)

**One canonical fact envelope — the `FactClaim` — flows unchanged through every
stage; stages differ only by which optional sub-objects are *populated*, governed
by a single `resolution` enum, not by a different schema.** I reject both extremes:

- *Not* truly stage-specific schemas (extractor-shape, integrator-shape,
  review-shape as separate types). That multiplies contracts, mappings, and
  drift, and re-litigates the §3 "version & migrate" tension three times.
- *Not* a single flat blob that pretends extraction and integration are the same
  act. Extraction genuinely does not know entity IDs; integration genuinely does.

Instead: **one envelope, monotonically enriched.** A `FactClaim` is born at
extraction with mention-level references and *no* resolved IDs (`resolution:
"mention"`). Integration enriches the *same object* in place — it fills
`subject.entity_id`, fills `object.entity_id` for relationship facts, attaches the
canonical predicate, and flips `resolution: "resolved"`. The review payload is the
same object at `resolution: "resolved"` (or `"held"`); the human never sees a
fourth shape. **Edits are not part of the fact shape** — they are operations
*against* a `FactClaim` (Track C owns that algebra); I only guarantee that every
field a human may correct is an addressable, typed field on the envelope.

The two load-bearing inventions:

1. **A `TypedValue` union** so a value is *never* a sentence. Every value is one
   of seven discriminated variants (`enum | quantity | date | boolean | text |
   structured | ref`). `ref` is the relationship case and is structurally
   forbidden from also carrying a literal — an edge is a value, not a string.
2. **A `slot` object that makes cardinality intent explicit in-flight.** Each
   claim declares `slot.cardinality` (`functional | set`) and, when it is a
   correction/update rather than a first assertion, `slot.merge` (`assert |
   add | remove | replace`). "Add vs replace" stops being inferred downstream;
   it is a typed field the producer (extractor default, integrator override,
   human correction) sets.

---

## 2. Concrete strawman IR

### 2.1 The envelope

```jsonc
// FactClaim — the SINGLE shape, all stages. *_optional fields populate by stage.
{
  "schema": "factclaim/1",              // contract version (semver-major in the tag)
  "claim_id": "fc_01HZ...",             // ULID, minted at extraction, stable across enrichment
  "resolution": "mention",              // mention | resolved | held | committed  (monotone)

  "subject": { /* Ref */ },             // who/what the fact is about
  "predicate": { /* Predicate */ },
  "value": { /* TypedValue */ },        // the literal OR an edge (ref variant); never both
  "slot": { /* Slot */ },               // cardinality + merge intent (the array core)

  "modality": "asserted",               // asserted|negated|hypothetical|reported|question|expected
  "kind": "attribute",                  // event|measurement|state|attribute|preference|relationship
  "domain": "general",                  // general|health|finance|location  (Track F enforces; I carry)

  "confidence": 0.82,                   // model's calibrated [0,1]; null if not produced
  "provenance": { /* Provenance */ },   // mandatory, even at extraction
  "temporal": { /* Temporal */ },       // Track G owns the shape; I carry an opaque-but-typed slot
  "notes": null                         // free-text rationale the model may emit; NEVER the value
}
```

`resolution` is monotone: `mention → resolved → held → committed`. A stage may
advance it; nothing moves it backward except an explicit reopen op (Track C).
Validators key off it: at `mention`, `entity_id` MUST be null and `mention` MUST
be present; at `resolved`, `subject.entity_id` MUST be non-null (object too, iff
`kind == relationship`). This is the "deterministic backstop" Track D consumes.

### 2.2 `Ref` — entity reference, pre- vs post-resolution

The same object models both states; a `mention` is never overwritten when the
`entity_id` is filled — both coexist so provenance and re-resolution stay possible.

```jsonc
// Pre-resolution (extraction emits this):
{
  "mention": {
    "surface": "Sam",                   // verbatim span text
    "span": { "start": 41, "end": 44 }, // char offsets into the source chunk
    "kind_hint": "person"               // model's guess; advisory only
  },
  "entity_id": null,
  "candidate_ids": []                   // optional: integrator may stage ranked candidates pre-commit
}

// Post-resolution (integration fills entity_id; mention is RETAINED):
{
  "mention": { "surface": "Sam", "span": { "start": 41, "end": 44 }, "kind_hint": "person" },
  "entity_id": "ent_7f3a",
  "candidate_ids": ["ent_7f3a", "ent_9b1c"]   // audit: what else it could have been
}

// Mint-new intent (human or integrator asserts "this is a NEW entity"):
{
  "mention": { "surface": "Dr. Okafor", "span": {"start":10,"end":20}, "kind_hint": "person" },
  "entity_id": null,
  "mint": { "kind": "person", "reason": "no candidate above threshold" }
}
```

Rationale for retaining the mention forever: re-resolution (wishlist §2.3/§2.4
"which Sam"), audit ("the model meant the span at 41–44"), and identity ops
(Track B's split/merge need the original surface). This mirrors EL practice where
mention detection and disambiguation are distinct, retained stages.

### 2.3 `TypedValue` — the seven-variant discriminated union (a value is never a sentence)

```jsonc
// enum — bounded vocabulary, validated against the predicate's enum_values
{ "type": "enum", "code": "married", "label": "Married" }

// quantity — number + UCUM-style unit; the {value,unit} shape from the registry
{ "type": "quantity", "value": 5.4, "unit": "mmol/L", "precision": 0.1 }

// date — typed temporal LITERAL (distinct from the fact's validity, which is `temporal`)
{ "type": "date", "value": "1984-03-12", "grain": "day" }   // grain: instant|day|month|year|era|unknown

// boolean
{ "type": "boolean", "value": true }

// text — the ONLY free-text variant; bounded and tagged, for genuinely-unstructured atoms
{ "type": "text", "value": "anaphylaxis", "lang": "en" }

// structured — a typed record for a predicate whose `value_shape: structured`
{ "type": "structured", "shape": "address", "fields": {
    "line1": "12 Elm St", "city": "Austin", "region": "TX", "postal": "78701" } }

// ref — THE RELATIONSHIP CASE. The "value" is an edge to an entity, not a literal.
{ "type": "ref", "ref": { /* a full Ref object, §2.2 */ }, "role": "employer" }
```

Hard rule: **`type: ref` carries NO scalar `value` field, and the six literal
variants carry NO `ref`.** This makes "is this fact a relationship?" a structural
question (`value.type == "ref"` ⟺ `kind == "relationship"` ⟺ object entity link
exists), not a guess. The predicate registry's declared `value_shape`
(`scalar|text|enum|quantity|date|ref|structured` — already in the YAML per
`PREDICATE_CANONICALIZATION.md` §3.2) is the *expected* variant; a mismatch routes
to a shape-mismatch review (never silently drops), exactly as that doc specifies.

A genuinely free-form note the model wants to attach goes in the envelope's
`notes`, **never** in `value`. The validator rejects a `text` value longer than a
short bound (e.g. 120 chars) for any predicate whose `value_shape` is not `text` —
the deterministic backstop that kills "value = whole sentence."

### 2.4 `Predicate` + `Slot` — cardinality and add/replace, in-flight

```jsonc
"predicate": {
  "raw": "worksFor",                    // verbatim from the model
  "canonical": "person.employer",       // filled by integrator canonicalization; null at extraction
  "qualifier": { "audience": "close_friends" },  // typed qualifier map; e.g. nickname audience
  "value_shape": "ref"                  // expected variant, copied from registry at resolution
}

"slot": {
  "cardinality": "set",                 // functional | set  (from registry `functional` flag)
  "merge": "add",                        // assert | add | remove | replace
  "slot_key": "person.employer"          // the addressable multi-value bucket
}
```

`cardinality` comes from the registry's `functional` flag (the doc notes this is
currently a hardcoded allowlist drifting from YAML — the new IR forces it to be
registry-driven). `merge` encodes the §2.9 core:

- `assert` — first/normal assertion; integrator decides based on `cardinality`
  (functional ⇒ supersede head; set ⇒ accumulate). The *safe default*.
- `add` — explicitly append to a set (a second employer, a third child).
- `remove` — retract one member of a set without touching the rest.
- `replace` — supersede the current head even for a set predicate (rare;
  human-driven "no, replace ALL my phone numbers with this").

Why model `merge` in-flight rather than only at the human-edit layer: the
extractor often *knows* intent ("she ALSO works for…") and the integrator must
not have to re-infer it from prose. For set predicates, `add` vs `replace` is
otherwise unrecoverable downstream — the exact failure §2 names. Track C's edit
ops map cleanly onto these four verbs, so there is one vocabulary, not two.

### 2.5 Provenance & temporal (carried, typed, mandatory)

```jsonc
"provenance": {
  "note_id": "note_abc",
  "chunk_id": "chunk_3",
  "span": { "start": 18, "end": 67 },   // the SENTENCE span supporting the whole claim
  "extractor": "factclaim/1@grok",      // model + contract version
  "captured_at": "2026-06-16T14:00:00Z" // reported-time anchor (bitemporal; Track G/B own depth)
}
// temporal: I carry a typed slot; Track G owns its internal shape. Shown for completeness:
"temporal": {
  "valid_from": { "value": "2019-01", "grain": "month" },
  "valid_to": null,                      // null = ongoing
  "status": "ongoing",                   // ongoing | former | ended | unknown
  "recurrence": null                     // rrule string, Track G
}
```

Provenance is mandatory at *every* stage including extraction (success criterion:
"provenance to a note/span"). The claim span and the value's date literal are
distinct fields so "born in 1984" doesn't collapse validity-time and value.

### 2.6 The hard cases, end-to-end

**Multi-valued / "my daughters Summer, Harmony, Lydian" (split + add):**
extraction emits three `FactClaim`s sharing a `split_group` id, each
`kind: relationship`, `value.type: ref`, `slot: {cardinality:"set", merge:"add"}`,
each with its own mention span. No ambiguity that these accumulate.

```jsonc
{ "claim_id":"fc_a","split_group":"sg_1","subject":{"mention":{"surface":"my"...}},
  "predicate":{"raw":"daughter","canonical":"person.child"},
  "value":{"type":"ref","ref":{"mention":{"surface":"Summer","span":{"start":14,"end":20}}},"role":"child"},
  "slot":{"cardinality":"set","merge":"add","slot_key":"person.child"},
  "kind":"relationship","modality":"asserted" }
// ...fc_b (Harmony), fc_c (Lydian) identical but for surface/span.
```

**Negation — "Sam is NOT allergic to penicillin":** same shape, `modality:
"negated"`. The value still types normally (`{"type":"text","value":"penicillin"}`
or a `ref` to a substance entity); negation is a property of the *assertion*, not
the value. This follows clinical-NLP assertion practice (certainty is an axis on
the concept, not a mutation of it).

**Hypothetical — "if I switch to Acme next year":** `modality: "hypothetical"`,
`temporal.valid_from` in the future, `confidence` low. A hypothetical is *carried*
into integration (so the human can later confirm/deny it) but Track B/C decide it
does not assert into the live graph floor until promoted.

**Typed value vs sentence — "my A1c was 5.4":** `predicate.canonical:
"health.a1c"`, `value:{"type":"quantity","value":5.4,"unit":"%"}`, `domain:
"health"`, `kind:"measurement"`. The validator rejects any attempt to emit
`value:{"type":"text","value":"my A1c was 5.4"}` for this predicate.

---

## 3. Rationale

1. **One envelope, monotone resolution** beats stage-specific schemas because the
   §3 "version & migrate" problem is then solved *once*: a single `schema`
   discriminator, one validator family, one migration path. Stage differences are
   data (which fields are filled), not type — eliminating N×N mapping code and the
   drift it breeds. This is the same instinct as FHIR's single resource with
   profile-constrained slices, rather than separate request/response types.
2. **`TypedValue` as a discriminated union** is the direct, structural answer to
   "a value is never a sentence." It generalizes schema.org's split between a
   structured `value` and a human-readable `description`: we keep only the
   structured side as the *value*, and shunt prose to `notes`. The seven variants
   map 1:1 onto the registry's existing `value_shape` enum, so the contract and
   the registry cannot drift.
3. **`ref` as a value variant** unifies attributes and relationships under one
   shape while keeping them structurally distinguishable — the property-graph
   literature's exact pain point ("to add info to a property you must promote it
   to an edge") is dissolved by making *every* value capable of being an edge from
   birth. No restructuring on relink.
4. **Explicit `slot.merge`** is the cheapest possible fix for "override vs array":
   it moves a single enum field upstream to whoever has the intent, instead of
   forcing downstream inference from prose. RDF-star / reification teaches that
   statement-level metadata (here: merge intent, modality, confidence) belongs
   *on the statement*, attached, not flattened into the triple.
5. **Mention retained post-resolution** keeps re-resolution, audit, and identity
   ops possible without a second round-trip to the source — EL best practice keeps
   mention and disambiguation as separate, both-retained stages.

---

## 4. Tradeoffs & alternatives considered

| Decision | Chosen | Rejected alternative | Why |
|---|---|---|---|
| Stage shapes | one monotone envelope | distinct per-stage schemas | one validator/migration; less mapping drift. Cost: optional-field sprawl, mitigated by `resolution`-keyed validation. |
| Value model | discriminated 7-union | single `value` + separate `value_type` string | union is self-validating and makes illegal states (ref+literal) unrepresentable. |
| Relationship | `ref` value variant | separate `object` top-level field always present | one value slot; `kind`/`value.type` stay in lockstep; no "object null for attributes" noise. |
| Multi-valued | array of one-claim-per-value + `slot.merge` | a single claim with a value *array* | one-edge-per-value makes per-value provenance, modality, and remove/relink atomic; arrays force all-or-nothing edits. Aligns with property-graph "promote to edges." |
| Merge intent | typed `merge` enum in-flight | infer add/replace at integration | inference is lossy for sets; the producer often knows. |
| Edits | ops *against* the envelope (Track C) | corrected-full-record replacement | ops give audit + reversibility (success criterion); full-record loses *which* field changed. I only guarantee field addressability. |

**Notable cost accepted:** the envelope is *fat* — many optional sub-objects.
Mitigation: `resolution`-keyed required-field validation makes "fat but
under-filled" detectable, and Track D's schema-constrained generation only has to
emit the `mention`-stage subset (no IDs, no canonical), which is the small part.

---

## 5. Risks / failure modes

- **R1 — Union overwhelms the extractor.** Seven value variants may exceed what a
  schema-constrained LLM emits reliably (Track D's domain). *Mitigation:* at
  extraction the model picks `type` only as a hint; the integrator re-types
  against the registry's `value_shape` (authoritative). The model being wrong
  about `type` is a recoverable mismatch-review, not corruption.
- **R2 — `merge` intent hallucinated.** Model emits `replace` when it meant `add`,
  silently superseding a set member. *Mitigation:* `assert` is the only default
  the model may emit; `add`/`remove`/`replace` require either an explicit prose
  cue the integrator re-validates, or a human edit. Functional-vs-set comes from
  the registry, not the model.
- **R3 — Mention spans drift / hallucinate.** Model invents `span` offsets that
  don't match the chunk. *Mitigation:* deterministic backstop verifies
  `chunk[start:end] == surface`; mismatch → low-trust, routes to review. Spans are
  validated, never trusted.
- **R4 — `resolution` monotonicity violated** by a buggy stage moving backward,
  silently dropping resolved IDs. *Mitigation:* monotone-only transitions enforced
  by the validator; backward moves require an explicit reopen op with audit.
- **R5 — `ref`/`kind` divergence** (a `ref` value with `kind != relationship`, or
  vice versa). *Mitigation:* cross-field invariant checked deterministically;
  it's the single most important consistency rule.
- **R6 — Domain on the wrong field enables a firewall leak** (Track F's concern,
  but my shape carries `domain`). *Mitigation:* `domain` is a closed enum on the
  envelope, set by the integrator from the predicate's registry domain, never
  free-text; a relink that crosses firewalls must re-derive `domain`, not inherit.
- **R7 — Contract version skew** between an extractor emitting `factclaim/1` and
  an integrator expecting `/2`. *Mitigation:* `schema` is mandatory; a registry of
  per-version up-migrations (pure functions) runs at the integration boundary; no
  silent acceptance of an unknown version.

---

## 6. Positions on the §3 cross-cutting tensions (my lane)

- **One shape vs stage-specific:** **One monotone envelope.** Defended in §1/§3.
  Stage identity lives in `resolution` + which optionals are filled, not in the
  type. This is my strongest position and the red-team's biggest target.
- **Multi-valued representation end-to-end:** **one-claim-per-value (arrays of
  claims), never a value-array inside one claim**, plus a typed `slot.merge`.
  Per-value provenance/modality/remove become atomic; "add vs replace" is an
  explicit enum at every stage, set by the most-informed producer.
- **Edits — op-log vs corrected-record vs prose:** out of my lane to *own*
  (Track C), but my contract takes a side: **every correctable field is an
  addressable, typed path on the envelope, and `slot.merge`'s four verbs are the
  natural target vocabulary for set-valued ops** — so the edit algebra should be a
  typed op-log over these paths, not a corrected-blob diff. I expose the surface
  that makes an op-log clean.
- **Version & migrate:** **mandatory `schema` discriminator + pure up-migration
  functions at the integration boundary**, one family because there is one shape.
  No silent drift: an unknown version is a hard error, not a best-effort parse.
- **#7 wiki doctrine (boundary note):** my envelope makes the doctrine *easier* to
  keep — because a human correction is an op that produces a new/updated
  `FactClaim` with `provenance.extractor = "human-correction"` and an audit link,
  i.e. a machine-applied correction operation, not a prose edit. I assert the
  doctrine is preservable; Track C must prove the op algebra; the red-team attacks
  it.

---

## 7. Open questions for the red-team

1. **Is the fat single envelope a false economy?** Does `resolution`-keyed
   validation actually prevent under-filled claims slipping through, or do we end
   up with three de-facto schemas wearing one type's clothes — worse than three
   honest ones?
2. **Can the LLM reliably pick among 7 value variants** at extraction, or should
   extraction emit an *untyped* value-with-hint and let the integrator be the sole
   typer (R1)? Where exactly does typing authority sit? (Hands to Track D.)
3. **Is `slot.merge` on the model's output a footgun (R2)?** Should the model be
   *forbidden* from emitting anything but `assert`, with `add/remove/replace`
   reserved entirely for the integrator + human layers?
4. **`split_group` for split-at-extraction vs split-at-review** — should the
   extractor ever pre-split ("Summer, Harmony, Lydian"), or always emit one claim
   and leave splitting to Track C/E so there is one split path, not two?
5. **Negation + multi-valued interaction:** "Sam no longer works for A, B, or C" —
   is that three `merge:"remove"` negated claims, or one negated claim over a set?
   My current answer is three; is that ergonomic?
6. **Does retaining `mention` forever create a firewall/PII surface** (the verbatim
   health span lives on a fact that may be relinked to `general`)? Hand-off to
   Track F: should mention spans be redacted/scoped on domain move?
7. **`structured` value variant — escape hatch or hole?** It's the one place a
   value could smuggle arbitrary nesting. Should every `structured.shape` be
   registry-declared (closed set) or may the model coin shapes (open)? I lean
   closed; confirm.
