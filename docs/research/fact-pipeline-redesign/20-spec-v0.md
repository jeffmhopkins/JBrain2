# Integrated spec v0 — fact pipeline & review redesign (synthesis)

**Status:** SYNTHESIS v0, for adversarial red-team. NOT signed off.
**Inputs:** `00-framing.md` (binding) + tracks A (fact IR), B (storage),
C (corrections), D (prompt/extraction), E (review), F (security), G (temporal).
**Reading instruction for the red-team:** this is an *integration*, not a
concatenation. Where tracks disagree, §7 names the conflict, the options, a
*provisional* pick, and the evidence that would flip it. The seams are left
visible on purpose — attack them.

---

## 1. Overview & design spine

Five load-bearing through-lines hold the whole design together. Every track
either asserts one of these or is constrained by it.

1. **Model proposes / a deterministic committer decides.** The extraction LLM
   reads attacker-influenceable prose and emits *structured intent only*; it has
   no write capability. A single privileged, deterministic **committer** (Track
   C's "arbiter", Track F's "committer", Track D's "validator→repair→gate" — the
   *same* component viewed from three lanes) validates against registry + value
   shapes + firewall rules, then machine-writes. **Schema-valid ≠ correct**
   (D §0): constrained decoding guarantees the JSON parses; only the deterministic
   pass guarantees grounding, link safety, and firewall safety. This split is
   simultaneously the reliability story (D), the #7-doctrine story (A/C/E/F/G),
   and the prompt-injection defense (F's CaMeL-style dual-LLM boundary).

2. **Op-log audit is the single mechanism for change, audit, and undo.** Every
   mutation — model-proposed or human-proposed — is an append to one typed
   operation log (`fact_op` / `fact_ops`), applied transactionally with an
   immutable audit row that carries a precomputed inverse. The op-log *is* the
   change feed, the audit trail, and the undo stack (B §4, C §2.5, F §2.2). There
   is no second bookkeeping system to drift, and no direct write path to the
   graph for any actor.

3. **Cardinality lives in identity, not in branchy application code.** A
   predicate's `functional | set` flag (from the predicate registry) determines
   the *identity key* of a fact slot: functional predicates key *without* the
   value (a new value supersedes), set-valued predicates key *with* a stable
   `value_identity` (a new value is a peer). "Override vs. add" is therefore a
   data property, decided once at key computation, not an `if functional then
   override else add` branch anyone can get wrong (B §1.2/§3.1). This same flag
   drives which correction ops are *legal* (C §3) and which editor the review card
   renders (E §3.2).

4. **#7 (machine-written wiki) is preserved through-line, not relaxed.** A human
   never writes a fact row or article prose. They emit a *typed correction
   operation*; the committer validates and machine-writes; the wiki re-derives
   from facts on the next pass. All five mutating tracks (A, C, E, F, G)
   independently land on this position. The *only* soft edge is `add_fact`
   (human-originated content the extractor never produced) — flagged explicitly in
   §4 and §7(e).

Design spine in one line: **a single fact envelope is proposed by the model,
hardened by deterministic backstops, committed append-only with cardinality
baked into its identity key, and corrected only through a typed, audited,
reversible op-log that the machine applies — keeping the wiki machine-written.**

---

## 2. The fact contract — canonical `FactClaim` envelope

Adopting Track A's **single monotone envelope** (`mention → resolved → held →
committed`), with Track G's `temporal` object slotted in verbatim and Track B's
identity mapping noted at each field. One shape, all stages; stages differ only by
which optional sub-objects are populated and by the `resolution` enum.

### 2.1 The envelope

```jsonc
{
  "schema": "factclaim/1",          // contract version (D: pinned const; A: semver-major tag)
  "claim_id": "fc_01HZ...",         // ULID minted at extraction, stable across enrichment
  "resolution": "mention",          // mention | resolved | held | committed  (monotone)
  "split_group": null,              // ULID shared by claims split from one sentence (A §2.6)

  "subject":   { /* Ref */ },
  "predicate": { /* Predicate */ },
  "value":     { /* TypedValue */ },// a literal OR an edge (ref variant); NEVER both
  "slot":      { /* Slot */ },      // cardinality + merge intent (the array core)

  "modality": "asserted",           // asserted|negated|hypothetical|reported|question|expected
  "kind":     "attribute",          // event|measurement|state|attribute|preference|relationship
  "domain":   "general",            // general|health|finance|location  (F enforces; A carries)

  "confidence": 0.82,               // model raw; committer clamps+recalibrates (D §F1)
  "provenance": { /* Provenance */},// mandatory at EVERY stage
  "temporal":   { /* Temporal */ }, // Track G object, verbatim (§2.5)
  "process": {                      // D §4.1 "provenance of process" (distinct from source)
    "extractor_version": "factclaim/1",
    "prompt_version": "stage2/v3",
    "validator_version": "val/v3",
    "model_id": "<adapter-resolved>",
    "repaired_by": []               // which backstops fired (D §E3); [] = untouched
  },
  "notes": null                     // model rationale; NEVER the value
}
```

`resolution` is monotone; only an explicit reopen op moves it backward (A §2.1,
B §4). Validators key required-fields off it: at `mention`, `entity_id` MUST be
null and `mention` MUST be present; at `resolved`, `subject.entity_id` MUST be
non-null (object too iff `kind == relationship`).

> **Storage mapping (B §6.2):** the persisted `fact_assertion` row is *narrower
> and stricter* than this envelope — a one-way, lossless-upward projection
> (provenance retains the original span). The envelope is the wire/IR shape; the
> row is the committed shape. This is a deliberate divergence from A's "one shape
> everywhere" claim at the storage boundary — see §7(b).

### 2.2 `Ref` — entity reference (pre/post-resolution, mention retained)

```jsonc
// extraction (mention-level, no id):
{ "mention": { "surface": "Sam", "span": {"start":41,"end":44}, "kind_hint":"person" },
  "entity_id": null, "candidate_ids": [] }

// integration (id filled, mention RETAINED for re-resolution/audit):
{ "mention": { "surface":"Sam","span":{"start":41,"end":44},"kind_hint":"person" },
  "entity_id": "ent_7f3a", "candidate_ids": ["ent_7f3a","ent_9b1c"] }

// mint intent:
{ "mention": { "surface":"Dr. Okafor","span":{"start":10,"end":20},"kind_hint":"person" },
  "entity_id": null, "mint": { "kind":"person","reason":"no candidate above threshold" } }
```

> **Security overlay (F §2.3):** `entity_id` resolves to an **entity projection in
> the fact's own domain**, NOT a global entity row. A's `entity_id` and B's
> `entity.entity_id` are reconciled with F's projection model in §3 and §7(a) —
> this is a live CONFLICT.

### 2.3 `TypedValue` — seven-variant discriminated union (a value is never a sentence)

```jsonc
{ "type":"enum",     "code":"married", "label":"Married" }
{ "type":"quantity", "value":5.4, "unit":"mmol/L", "precision":0.1 }
{ "type":"date",     "value":"1984-03-12", "grain":"day" }   // value literal, NOT validity
{ "type":"boolean",  "value":true }
{ "type":"text",     "value":"anaphylaxis", "lang":"en" }    // only free-text variant; bounded
{ "type":"structured","shape":"address","fields":{ "line1":"12 Elm St","city":"Austin" } }
{ "type":"ref",      "ref": { /* a full Ref, §2.2 */ }, "role":"employer" }   // the relationship case
```

Hard rules (A §2.3): `type:ref` carries no scalar value; the six literal variants
carry no ref. `value.type == "ref"` ⟺ `kind == "relationship"` ⟺ object link
exists (cross-field invariant, A R5). The registry's `value_shape` is the
*expected* variant; a mismatch routes to shape-mismatch review, never a silent
drop. **Typing authority is a CONFLICT** — A lets the model pick `type` as a hint
and the integrator re-type; D wants the deterministic validator to re-derive the
typed value from the cited span (B2). See §7(c). Provisional reconciliation:
**model emits `type` + verbatim `raw`; the validator re-derives the typed value
deterministically from `raw` and the parse wins ties** (D's B2 authority), with
the registry `value_shape` as the expected-shape gate.

### 2.4 `Predicate` + `Slot` — cardinality & add/replace in-flight

```jsonc
"predicate": {
  "raw":"worksFor", "canonical":"person.employer",  // canonical filled by integrator; null at extraction
  "qualifier":{ "audience":"close_friends" },
  "value_shape":"ref"                               // expected variant, copied from registry
}
"slot": {
  "cardinality":"set",        // functional | set  — from registry `functional` flag, NOT the model
  "merge":"add",              // assert | add | remove | replace
  "slot_key":"person.employer",
  "value_identity": null      // B §3.2: object_id | natural key | minted id; set for set-members
}
```

`assert` is the only `merge` the model may emit (A R2/§6, D C3); `add/remove/
replace` are reserved for integrator-with-cue or human ops. `cardinality` is
stamped deterministically from the registry, never trusted from the model (D C3).

### 2.5 `Provenance` + `Temporal` (Track G object, verbatim)

```jsonc
"provenance": {
  "note_id":"note_abc", "chunk_id":"chunk_3",
  "span":{ "start":18, "end":67 },        // SENTENCE span supporting the claim
  "quote":"started at Acme in March 2021",// denormalized for audit stability (B §3.4)
  "source_kind":"extracted",              // extracted | human_correction | human_assertion | agent
  "captured_at":"2026-06-16T14:00:00Z"
}

"temporal": {                              // Track G g-temporal/1, carried as a typed sub-object
  "schema_version":"g-temporal/1",
  "valid_from": { "instant":"2019-09", "precision":"month", "certainty":"asserted", "bound":"closed" },
  "valid_to":   { "instant":null, "precision":"unknown", "certainty":"asserted", "bound":"open" },
  "status":"ongoing", "status_reason":"valid_to.bound=open && valid_from<=now",
  "recurrence": null
}
```

The **bound trichotomy** `closed | open | unknown` (G §2.2) is binding: `unknown`
end = "former without a date", `instant:null`, excluded from current-value,
rendered as a word. This is the structural fix for the `— → 2026` complaint.
`reported_at`/`recorded_at`/`tx_[from,to)` are the bitemporal envelope and live on
the **storage row**, not the value (G §2.5) — A's `provenance.captured_at` is the
same anchor as G's `reported_at`; §7 notes the minor naming seam.

### 2.6 Consolidated worked examples

**(i) Typed value — "my A1c was 5.4":**
```jsonc
{ "schema":"factclaim/1","claim_id":"fc_a1c","resolution":"resolved",
  "subject":{"mention":{"surface":"my","span":{"start":0,"end":2}},"entity_id":"ent_self"},
  "predicate":{"raw":"A1c","canonical":"health.a1c","value_shape":"quantity"},
  "value":{"type":"quantity","value":5.4,"unit":"%"},
  "slot":{"cardinality":"set","merge":"add","slot_key":"health.a1c","value_identity":"vi_min_01"},
  "modality":"asserted","kind":"measurement","domain":"health",
  "temporal":{"valid_from":{"instant":"2026-06","precision":"month","bound":"closed"},
              "valid_to":{"instant":"2026-06","precision":"month","bound":"closed"},"status":"ended"},
  "provenance":{"note_id":"note_h","span":{"start":18,"end":30},"quote":"my A1c was 5.4","source_kind":"extracted"} }
```

**(ii) Relationship link — "Sam works for Acme":**
```jsonc
{ "schema":"factclaim/1","claim_id":"fc_emp","resolution":"resolved",
  "subject":{"mention":{"surface":"Sam","span":{"start":0,"end":3}},"entity_id":"ent_sam"},
  "predicate":{"raw":"worksFor","canonical":"person.employer","value_shape":"ref"},
  "value":{"type":"ref","ref":{"mention":{"surface":"Acme","span":{"start":14,"end":18}},"entity_id":"ent_acme"},"role":"employer"},
  "slot":{"cardinality":"set","merge":"assert","slot_key":"person.employer","value_identity":"ent_acme"},
  "modality":"asserted","kind":"relationship","domain":"general",
  "temporal":{"valid_from":{"instant":"2021-03","precision":"month","bound":"closed"},
              "valid_to":{"instant":null,"precision":"unknown","bound":"open"},"status":"ongoing"} }
```
(Note `value_identity == ent_acme`: the object's id is the member identity, so
re-spelling Acme's name does not fork the set — B §3.2 priority 1.)

**(iii) Multi-valued split — "my daughters Summer, Harmony, Lydian":** three
claims sharing `split_group`, each `kind:relationship`, `value.type:ref`,
`slot:{cardinality:"set", merge:"add"}`, each its own mention span & minted
`value_identity`:
```jsonc
{ "claim_id":"fc_a","split_group":"sg_1","resolution":"resolved",
  "subject":{"entity_id":"ent_self"},"predicate":{"canonical":"person.child","value_shape":"ref"},
  "value":{"type":"ref","ref":{"mention":{"surface":"Summer","span":{"start":14,"end":20}},"entity_id":"ent_summer"},"role":"child"},
  "slot":{"cardinality":"set","merge":"add","slot_key":"person.child","value_identity":"ent_summer"},
  "kind":"relationship","modality":"asserted","domain":"general" }
// fc_b (Harmony), fc_c (Lydian) identical but for surface/span/entity_id/value_identity.
```

**(iv) Negation / hypothetical:**
```jsonc
// "Sam is NOT allergic to penicillin" — modality on the assertion, value types normally:
{ "predicate":{"canonical":"health.allergy","value_shape":"text"},
  "value":{"type":"text","value":"penicillin"},"modality":"negated","kind":"state","domain":"health" }
// "if I switch to Acme next year" — carried, not asserted into the live floor until promoted:
{ "predicate":{"canonical":"person.employer","value_shape":"ref"},
  "value":{"type":"ref","ref":{"mention":{"surface":"Acme"}}},"modality":"hypothetical",
  "temporal":{"valid_from":{"instant":"2027","precision":"year","bound":"closed"}},"confidence":0.3 }
```

**(v) Recurring — "PT every Tue/Thu through Dec 2026, skip Sep 8":**
```jsonc
{ "predicate":{"canonical":"health.therapy_session","value_shape":"text"},
  "value":{"type":"text","value":"physical therapy"},"kind":"event","domain":"health",
  "temporal":{
    "valid_from":{"instant":"2026-01-06","precision":"day","bound":"closed"},
    "valid_to":{"instant":"2026-12-31","precision":"day","bound":"closed"},"status":"recurring",
    "recurrence":{"rrule":"FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=2026-12-31","dtstart":"2026-01-06",
                  "rdates":[],"exdates":["2026-09-08"],"overrides":[],"tz":"America/Los_Angeles","count_cap":730}}}
```

**(vi) Former without a date — "used to work at Acme":**
```jsonc
{ "predicate":{"canonical":"person.employer","value_shape":"ref"},
  "value":{"type":"ref","ref":{"entity_id":"ent_acme"},"role":"employer"},
  "slot":{"cardinality":"set","merge":"assert","value_identity":"ent_acme"},
  "kind":"relationship","modality":"asserted","domain":"general",
  "temporal":{
    "valid_from":{"instant":"2019","precision":"year","bound":"closed"},
    "valid_to":{"instant":null,"precision":"unknown","certainty":"asserted","bound":"unknown"},
    "status":"former","status_reason":"valid_to.bound=unknown"}}
// valid_now=false ⇒ excluded from current-value; renders "former (since 2019)". No fabricated end.
```

---

## 3. Storage & graph model

Adopting Track B's **append-only, bitemporal, one-edge-per-value** store. Three
layers so identity, history, and "current truth" never fight (B §1.1):

- **Entity node / projection** — a thing facts point at; split/merge operate here.
- **Fact assertion** — the immutable append-only edge row; the audit grain and
  unit of reversibility; never UPDATEd in place.
- **Fact slot** — the *logical* fact: a derived identity key (`slot_key`) grouping
  assertions that are "the same fact over time", plus a partial index selecting
  the live row(s). Not a stored table by default (materialization is an optional
  cache, B §5.4).

### 3.1 Assertion table + slot key (B §3, temporal columns from G §2.5)

```sql
CREATE TABLE fact_assertion (
  assertion_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id       uuid NOT NULL,
  domain_id      int  NOT NULL REFERENCES domains(id),    -- firewall band; in slot key (B §3.1)
  slot_key       bytea NOT NULL,                           -- identity key, derived (§3.2)
  value_identity bytea,                                    -- set-member sub-identity; NULL for functional
  supersedes     uuid REFERENCES fact_assertion(assertion_id),
  op_id          uuid NOT NULL REFERENCES fact_op(op_id),  -- every row traces to one op (F: created_by_op)

  subject_id     uuid NOT NULL,                            -- entity projection in THIS domain (§3.4)
  predicate      text NOT NULL,                            -- canonical
  qualifier      text,                                     -- part of slot key
  value_json     jsonb,                                    -- typed literal per value_shape
  object_ref     uuid,                                     -- entity projection in THIS domain, iff value_shape=ref

  predicate_kind text NOT NULL,
  cardinality    text NOT NULL,        -- 'functional'|'set' — SNAPSHOT of registry at write (B §7-E)
  modality       text NOT NULL DEFAULT 'asserted',
  confidence     real, pinned boolean NOT NULL DEFAULT false,

  -- valid-time, structured per endpoint (G §2.5)
  valid_from_instant timestamptz, valid_from_precision text NOT NULL DEFAULT 'unknown',
  valid_from_bound   text NOT NULL DEFAULT 'open', valid_from_certainty text NOT NULL DEFAULT 'asserted',
  valid_to_instant   timestamptz, valid_to_precision text NOT NULL DEFAULT 'unknown',
  valid_to_bound     text NOT NULL DEFAULT 'open', valid_to_certainty text NOT NULL DEFAULT 'asserted',
  valid_range  tstzrange GENERATED ALWAYS AS (tstzrange(valid_from_instant,
                 CASE WHEN valid_to_bound='closed' THEN valid_to_instant ELSE NULL END,'[)')) STORED,
  recurrence   jsonb,                                      -- RFC-5545 (G §2.3)
  status       text,                                       -- derived cache (G §2.4), re-derivable

  -- bitemporal envelope
  reported_at  timestamptz,                                -- when the SOURCE asserted it
  recorded_at  timestamptz NOT NULL DEFAULT now(),
  tx_from      timestamptz NOT NULL DEFAULT now(),
  tx_to        timestamptz,                                -- NULL = currently believed
  state        text NOT NULL DEFAULT 'live'                -- live|superseded|retracted|tombstone
);
ALTER TABLE fact_assertion ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_assertion FORCE  ROW LEVEL SECURITY;
```

**Slot key (B §3.1):**
`slot_key = hash(owner_id, subject_id, predicate, COALESCE(qualifier,''),
domain_id [, value_identity IF cardinality='set'])`.

- Functional ⇒ value excluded ⇒ new value with same key *supersedes*.
- Set-valued ⇒ `value_identity` included ⇒ different members coexist.
- `value_identity` (B §3.2) = object projection id for `ref` (priority 1) →
  natural key (E.164 phone, lowercased email) → minted uuid carried across
  supersessions. **add** mints a new `value_identity`; **replace** reuses the
  member's existing one; **remove** tombstones it. This is what makes a typo-fix a
  supersession of *that* member, not a spurious add.
- Qualifier and domain participate, so a firewall move is a genuine *re-key* (a
  new slot), not an in-place mutation.

**Live-row selection** is a partial unique index `WHERE tx_to IS NULL AND
state='live'` (≤1 live row per slot; for sets the key already includes
`value_identity`, so it is ≤1 live row per member). Two independent gates:
`tx_to IS NULL` ("we still believe it") and the valid-time window ("true at the
queried instant") — the bitemporal split.

### 3.2 Op-log (B §4, F §2.2)

```sql
CREATE TABLE fact_op (
  op_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id   uuid NOT NULL,
  domain_id  int NOT NULL REFERENCES domains(id),  -- domain the op acts WITHIN; op-log is domain-scoped
  op_kind    text NOT NULL,        -- set_field|relink|retime|add_to_set|replace_member|remove_from_set
                                    -- |supersede|retract|split|merge|entity_split|entity_merge
                                    -- |pin|set_modality|move_domain|...
  actor      text NOT NULL,        -- 'human:owner' | 'extractor' | 'integrator' | 'reprocess' | 'agent'
  source     text NOT NULL,        -- principal class (F): drives op-allowlist
  target_slot bytea, target_fact uuid,
  payload    jsonb NOT NULL,       -- typed correction (C contract), schema-validated
  applied    boolean NOT NULL DEFAULT false,
  inverse_of uuid REFERENCES fact_op(op_id),  -- undo is itself an op
  batch_id   uuid,                 -- a review session = atomic, jointly-undoable group
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE fact_op ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_op FORCE ROW LEVEL SECURITY;
```

One transaction per op (op row + its assertion inserts commit together, B R7).
A `pinned` live row cannot be superseded by a `reprocess` op — only by human/agent
(wishlist §14). `fact_audit` (immutable, append-only via `REVOKE UPDATE,DELETE` +
trigger) records before/after snapshot + the precomputed inverse op (C §2.5).

### 3.3 CONFLICT — global entity + redirect (B) vs per-domain projection (F)

This is the largest unreconciled seam; both positions are presented and a
provisional position taken (full treatment in §7(a)).

**B's model:** one global `entity` table; split/merge are O(1) via
`entity.status` + `redirect_to`; reads resolve through redirects. Simple, cheap,
one identity per thing.

**F's model:** **no global entity row referenced cross-domain.** Instead
**per-domain `entity_projection`** (one row per `(canonical_entity, domain)`,
holding only that domain's name/attrs), threaded by an RLS-scoped
`entity_identity(canonical_id, projection_id, domain_id)`. Facts reference a
*projection in their own domain*, so no foreign key ever crosses a firewall (kills
the FK covert channel, the read-oracle, and the merge-leak — F attacks 2/3/5).

**Why this is a real conflict, not a layering difference:** B's `redirect_to` is
an FK between entity rows and B's `subject_id`/`object_id` FK a single shared
table; F forbids exactly that cross-domain FK. They cannot both be literally true.

**Provisional position:** **adopt F's per-domain projection as the storage shape;
re-express B's split/merge over projections + `entity_identity`.** Rationale:
F's model is the only one that satisfies the binding RLS invariant (the FK-bypass
leak is documented Postgres behavior, not theoretical), and the invariant is
non-negotiable. B's O(1) redirect split/merge survives *within* a domain
(`entity_projection.redirect_to` is intra-domain); cross-domain identity merge
becomes the gated `identity_merge` op (F §3). **Cost carried forward to §7(a):**
cross-domain entity resolution ("is this the same Dad?") now needs a privileged
step that briefly holds both domains' data (F open-Q 2) — a new high-value asset.

### 3.4 Provenance, redirects, bitemporal (B §3.4/§3.5/§6.4)

Provenance is per-assertion and immutable; correcting it supersedes with a new
span. Multi-note corroboration is an open question (B open-Q 6: add a provenance
row to the same assertion vs. supersede). Provenance stays **same-domain** (F
§2.4 rule 4): a general fact may never cite a health note span.

---

## 4. Correction algebra & the review submission

### 4.1 The op set (Track C, ~22 typed ops)

Every human correction is a typed, intent-bearing operation appended to the
op-log — never a free-form diff or corrected full record (C §1.2 rejects both
JSON-Patch and corrected-record). The closed algebra is what makes "fewer review
kinds" achievable (C §1.2, E §4). Groups (C §2.3):

- **A · per-field** — `set_field{predicate|qualifier|value*|modality|domain|
  kind|confidence}` (one super-op, field-discriminated), `retime` (whole temporal
  bundle, mapping to G's `set_bound/clear_bound/set_precision/mark_former/
  mark_ongoing/set_recurrence/...`).
- **B · entity-link** — `relink_subject`, `relink_object`, `mint_and_link_object`,
  `unlink_object`.
- **C · cardinality (the core)** — `add_to_set`, `remove_from_set`,
  `replace_head` (set-valued only).
- **D · structure** — `split_fact`, `merge_facts`, `add_fact`.
- **E · lifecycle** — `retract`/`unretract`, `supersede`, `pin`/`unpin`,
  `set_confidence`, `fix_provenance`.
- **F · identity** — `merge_entities`/`unmerge_entities` (non-destructive
  merge_link), `split_entity`, `assert_distinct`.

Each op is `op : GraphState → (GraphState, AuditRecord, InverseOp)`. The inverse
is precomputed and stored (RFC-6902 `test`/`preconditions` discipline for
invertibility + optimistic concurrency). `*set_field value` is **functional-only**
— a hard error on set predicates (C §3).

### 4.2 Functional-vs-set rule + offered_ops (C §3)

The registry `functional` flag deterministically selects legal value-ops:

| `functional` | legal value-ops | illegal (UI must not offer) |
|---|---|---|
| `true`  | `set_field value`, `supersede` | `add_to_set`, `replace_head`, `remove_from_set` |
| `false` | `add_to_set`, `replace_head`, `remove_from_set` | `set_field value` (hard error) |

`offered_ops` is computed **arbiter-side** from the registry, never by the client,
so a hostile/buggy client cannot smuggle `set_field value` onto a set predicate
(C §3.2, F §4.2). This is the same flag that drives the storage key (§3.1) and the
review card (§4.3) — one source of truth.

### 4.3 The review submission (reconcile C + E)

E's review card *is* a structured editor over one fact record; the human edits
fields and the card accumulates a **separate op list** (never mutating the read
payload in place). Two intentionally-asymmetric contracts: **fat read** (server →
card; the fact projection enriched with predicate metadata, ranked entity
candidates, enum domains, `ui_capabilities` firewall gates) and **thin write**
(card → server: one verdict + an ordered, typed op list — `ops` → `structure_ops`
→ `identity_ops`, applied in one transaction with a `base_version` optimistic-
concurrency token).

E's ops *are* C's algebra viewed from the consuming side; the names are
reconciled here to C's canonical set (e.g. E's `set_value`/`replace_value` map to
C's `set_field value`/`replace_head`; E's `set_valid_*` map to C's `retime` /
G's temporal ops). **The red-team should hold E's submission JSON and C's op
schema side by side and flag any op E emits that C does not define, or vice
versa** — the asymmetric fat-read/thin-write split risks value-shape drift unless
both derive from one shared value-shape schema (A) via codegen (E risk 7).

**The cardinality question (E §2 "one record with N cells" vs A/B
"one-claim-per-value"):** see §7(b). Provisional: **storage is one-edge-per-value
(B §6.3, non-negotiable); the review card may *present* a set-valued slot as one
record with N temporally-scoped cells, but the submission lowers each cell to a
member-targeted op (`add_to_set`/`replace_head`/`remove_from_set` on a
`value_identity`).** The "atom" is the same at storage; the card's "cells" are a
presentation grouping, not a storage unit.

### 4.4 The #7 position (binding)

**Structured field edits are machine-applied correction operations and require NO
doctrine change** — the unanimous position of A, B, C, E, F, G. The reading
(C §4.2, verbatim-binding): #7 forbids humans *authoring graph/wiki state by
hand*; it does not forbid humans *issuing typed correction ops the deterministic
committer validates and applies*. Compliant iff: (1) no direct write — committer
writes; (2) closed typed vocabulary; (3) audited + reversible with stored inverse;
(4) the wiki regenerates from facts (a correction op may *draft* a correction note
but never edits article prose).

**The one soft edge (C §4.4):** `add_fact` introduces human-originated content the
extractor never produced. Provisional: admit it with
`provenance.source_kind="human_assertion"` citing the correction op as source,
flagged + visibly attributed wherever it surfaces, with a watch-metric on count.
Whether this should instead be forced through a correction-note round-trip is
§7(e).

---

## 5. Extraction & reliability (Track D)

Extraction is a **compiler front-end, not a chat**: the LLM proposes a structured
AST; a deterministic pass is the sole authority on what leaves the stage.

**Two-stage prompt** (D §1.1): Stage 1 (cheap, high-recall) segments + emits
*verbatim* surface phrases + cues + span offsets — no typed values, no enums.
Stage 2 (focused, per candidate) types/links with the **canonical predicate slice
and entity candidates injected**, turning the two worst hallucination surfaces
(inventing a predicate, inventing an entity id) into multiple-choice. Both via the
LLM adapter (invariant); both schema/grammar-constrained at decode.

**Deterministic backstop catalogue is the authority** (D §3) — the only two
terminal states are *validated-commit* and *review*; never silently-wrong, never
silently-dropped:
- **A · structural** — schema re-validation, required-field-by-`resolution`,
  `contract_version` pin (rejects stale shapes — no silent drift).
- **B · grounding (anti-hallucination core)** — **B1 span verification** (value is
  a fuzzy substring of the cited span; re-anchor small offset drift; else review);
  **B2 typed-value re-derivation** (re-parse the typed value deterministically from
  `raw`; the parse wins ties — *this is the resolution of the value-typing-authority
  conflict §7(c)*); **B3 negation/modality cross-check** (lexicon; lower confidence
  + review, never auto-flip).
- **C · vocabulary** — enum coercion, predicate canonicalization (embedding
  registry; drift→canonical, coin-dedup), **C3 cardinality stamping from the
  registry, not the model**.
- **D · link/firewall (100% tested)** — entity existence *in current RLS scope*,
  **D2 firewall guard** (cross-firewall link → review with consequence surfaced),
  candidate-rank consistency, self-link guard.
- **E · repair/re-ask** — bounded N=2 re-asks with validator errors appended, then
  degrade to review; every repair annotated (`repaired_by`) and idempotent.
- **F · calibration** — confidence clamp + recalibration; route to review if >k
  backstops fire.

**Versioning/migration (D §4):** single pinned `contract_version`; SemVer
discipline (patch = additive/no re-extract; minor = required field with
deterministic backfill; major = model-needed **budgeted, audited re-analysis
migration** on the Phase-5 workflow engine — plan→budget-gate→shadow+diff→
cutover→rollback). Re-extraction is reproducible (E3) and diffable, so blast
radius is computable before it runs. **Pinned facts are immutable to migration
unless explicitly reviewed** (wishlist §14).

**Eval (D §5):** frozen human-adjudicated golden set; per-field semantic metrics
(not string-match) via bipartite alignment; **negated/hypothetical→asserted and
hallucinated-link have zero-tolerance gates**; backstop-ablation test proves the
net works; adversarial/injection slice ties to F. CI-gated with record/replay
cassettes (LLM faked in tests, invariant).

**Open seam (D open-Q 2, → §7(d)):** span-anchoring (B1) assumes the value is a
substring of the span — but INFERRED facts ("turned 40 last week" → birth year)
are not quoted.

---

## 6. Security model (Track F)

**Committer authority.** The deterministic committer is the *only* writer to
`fact_assertion`/`entity_projection` (direct DML revoked from the app role; tested
F-test 14). It ignores any `domain_id` the model *claims* and **re-derives domain
from the operands** (subject projection's domain, provenance note's domain); RLS
`WITH CHECK` is the second line. This is the CaMeL-style dual-LLM boundary: the
model touches untrusted prose and only proposes; the privileged committer decides.

**Op-allowlist by source (F §4.2).** The extractor may emit
`{set_field, add_to_set, relink(in-scope only), retime, ...}` but **never**
`{move_domain, identity_merge-across-domains, pin, retract-of-pinned}`. The
allowlist is the capability boundary; an injected "move all of Dad's facts to
general" is structurally unrepresentable in model output.

**Domain projections vs global entities (§3.3, §7(a)).** Per-domain
`entity_projection` + RLS-scoped `entity_identity`; facts reference same-domain
projections only → no cross-firewall FK (R4), enforcement at *materialization* not
just row visibility (R3): a general edge dereferences only the *general*
projection's name, never a health attribute.

**Domain-move gating (F §3).** Upgrade (general→protected) is lower-risk.
**Downgrade (protected→general)** is the high-blast action: (1) LLM can never emit
it; (2) owner principal only; (3) explicit non-batchable confirm showing exactly
which values become visible; (4) **re-derivation not relabel** — a copy-forward
that mints a new general fact + projection, cites the original as provenance,
marks the original `superseded`, audits in *both* domains, leaves the protected
row intact (reversible); (5) bounded blast radius — one fact (or an explicitly
enumerated owner-confirmed set), rate-limited, no wildcard; (6) cascade is
explicit — moving a fact never auto-moves its object or siblings.

**Operational RLS (F §4.5):** `SET LOCAL` only (never `SET` — pool-bleed),
`FORCE ROW LEVEL SECURITY` + no `BYPASSRLS` in the app path, `domain_id`-leading
composite indexes, sanitized constraint errors.

**Isolation-test obligations (F §5, CLAUDE.md rule 3).** Every new table
(`fact_assertion`, `fact_op`, `fact_audit`, `entity_projection`, `entity_identity`)
ships RLS isolation tests on real Postgres via testcontainers with two scoped
sessions: read isolation, `WITH CHECK` write rejection, unset-scope = zero
rows/no writes, FORCE-on, no cross-domain FK (schema introspection), relink/render
materialization isolation, domain-move (extractor-rejected, owner-only,
copy-forward, reversible), injected-op corpus, audit append-only + round-trip.

---

## 7. OPEN CONFLICTS & DECISIONS (the crux — for the red-team)

Each: **conflict · options · provisional pick + why · what would flip it.**

**(a) Global entity table + redirect (B) vs per-domain entity projections (F).**
*Conflict:* B persists one global `entity` row with `redirect_to` and facts
FK-reference a single shared table; F forbids any cross-domain FK and replaces the
global row with per-domain `entity_projection` + RLS-scoped `entity_identity`.
They are mutually exclusive at the FK level. *Options:* (i) B's global table
(simple, O(1) redirect merge, one identity per thing, but a documented FK-bypass
firewall leak); (ii) F's projection model (kills the FK channel + read-oracle +
merge-leak, but multiplies entity rows and makes cross-domain "same-Dad"
resolution require a privileged dual-domain step); (iii) hybrid — global
*canonical* node with NO attributes + per-domain projections holding all
renderable data (a global skeleton that carries no protectable value). *Provisional
pick:* **(iii) leaning (ii)** — per-domain projections are authoritative for all
renderable attributes; a global `canonical_id` exists only as an opaque,
attribute-free thread, never an FK target for facts. The RLS invariant is binding
and B's global-table FK violates it. *What would flip it:* if the privileged
cross-domain resolver (needed to decide identity merges) is itself shown to be an
unavoidable oracle (F open-Q 1/2), the projection model's security advantage
collapses and a simpler single-table-with-column-level-RLS may be reconsidered;
or a perf model showing projection proliferation breaks entity resolution recall.

**(b) One-claim-per-value (A/B) vs one-record-with-N-cells review view (E) — same
atom at different layers, or a real divergence?** *Conflict:* A and B insist the
storage atom is one assertion per set member (`value_identity` in the key);
E presents a set-valued fact as *one card record with N temporally-scoped cells*.
*Options:* (i) genuinely one storage row per member, card cells are pure
presentation that lower to member-targeted ops; (ii) E's "N cells" leak upward
into a value-array storage shape (which B §7-C rejects outright as the
override-vs-array bug). *Provisional pick:* **(i) — same atom, different layer.**
Storage is one-edge-per-value (binding, B §6.3); the card's cells are a grouping
for display and each cell serializes to an op on a `value_identity`. *What would
flip it:* if the red-team shows split/merge or per-cell supersession cannot
serialize cleanly from the cell presentation (E open-Q 3), the card model may need
to render literal one-edge-per-value rows instead of grouped cells.

**(c) Value-typing authority: model (A) vs deterministic span re-derivation (D).**
*Conflict:* A lets the model pick the `TypedValue` variant as a hint and the
integrator re-type against the registry; D's B2 backstop re-parses the typed value
deterministically from the cited span and makes the parser authoritative ("the
model's typed fields are hints that lose ties to the deterministic parse").
*Options:* (i) model-authoritative typing (trusts LLM unit/date parsing); (ii)
deterministic-parser-authoritative (D B2); (iii) parser-authoritative with a
model-disagreement→review escape for messy units/locale dates. *Provisional pick:*
**(iii) — parser wins, but irreconcilable disagreement routes to review** (D B2's
own escape). The model emits `type` + verbatim `raw`; the registry `value_shape`
gates expected shape; the parser re-derives and wins ties; a hard
parser/model conflict is a review item, not a silent correction. *What would flip
it:* D open-Q 3 — a class where the model is *more* right than the parser (messy
units, locale dates) and the parser "silently corrupts in the same direction as a
missing test." If ablation eval finds the parser net-harmful on any field class,
that class reverts to model-authoritative.

**(d) Span-anchoring vs INFERRED (non-quoted) facts' provenance.** *Conflict:*
D's B1 (the primary anti-hallucination defense) requires the value to be a
substring of the cited span — but legitimate inferred facts ("turned 40 last week"
→ birth year; "must have been after X") are not quoted, and G's
`certainty:"inferred"` explicitly exists. A hard B1 would reject every inferred
fact; a soft B1 re-opens the hallucination hole. *Options:* (i) hard B1, no
inferred facts (loses real facts); (ii) an `inferred` provenance flag exempt from
B1 (re-opens the hole D names); (iii) inferred facts allowed but
`certainty:"inferred"` + low confidence + mandatory review + a *derivation trace*
(which span(s) the inference is computed from, so it is still grounded, just not
substring-grounded). *Provisional pick:* **(iii)** — inferred facts carry
`source_kind` that exempts substring B1 but *requires* a cited derivation span and
auto-route to review at reduced confidence, so they are checkable even if not
quoted. *What would flip it:* if the derivation trace proves unreliable (models
fabricate the derivation as readily as the value), revert to (i) and force
inferred facts through human `add_fact`.

**(e) `add_fact` as a first-class human op (C) vs forced correction-note (C/F).**
*Conflict:* C's `add_fact` lets a human introduce content the extractor never
produced — brushing #7's "machine-written" spirit even through the arbiter; the
alternative forces every human-originated fact through a correction *note* that
round-trips the extractor. *Options:* (i) `add_fact` with mandatory
`human_assertion` provenance + visible attribution + watch-metric (C's lean); (ii)
forced correction-note round-trip (purest #7, worse ergonomics for "the model
missed my daughter's name"); (iii) `add_fact` allowed but the note is
auto-drafted and the fact stays `provisional` until the next extraction pass
confirms or the owner re-affirms. *Provisional pick:* **(i) with the (iii)
auto-drafted note** — direct `add_fact`, hard `human_assertion` provenance flag,
auto-drafted correction note for the wiki loop, and a count metric the red-team
can watch. *What would flip it:* if human-asserted facts proliferate or the
red-team shows the attribution is droppable downstream (so a human-asserted fact
can masquerade as machine-extracted), force (ii).

**(f) Domain-move reversibility / laundering (C vs F).** *Conflict:* C models
`set_field domain` as a reversible field edit; F insists a downgrade is NOT a
relabel but an owner-only copy-forward, and even then warns undo may not be safe
once the fact was read/cited across the boundary (C risk 2, F open-Q 3). *Options:*
(i) reversible `set_field domain` (C's uniform field-edit framing — but a flip-in-
place orphans the same-domain projection/provenance invariants and may launder via
move+undo+retime); (ii) F's copy-forward downgrade op, reversible-by-retract-of-
the-general-copy but flagging downstream cites for re-evaluation; (iii) one-way
downgrade — no undo at all once general, a new fact must be authored to re-protect.
*Provisional pick:* **(ii) — downgrade is a distinct owner-only copy-forward op
(NOT `set_field domain`), reversible by retracting the general copy, with downstream
cites flagged on undo.** F's security argument is binding; C's `set_field domain`
is demoted to upgrade-only / re-expressed as the gated op for downgrade. *What
would flip it:* if the red-team demonstrates a move→undo→retime laundering sequence
that survives the copy-forward + both-domain audit (F open-Q 3), downgrade becomes
**(iii) one-way**.

**(g) Is the `set_field` super-op hiding firewall risk (C/F)?** *Conflict:* C
merges predicate/qualifier/modality/**domain**/kind/confidence into one
field-discriminated `set_field` for "fewer kinds"; F's whole model depends on
`domain` moves being a *distinct, hard-gated, owner-only* op — and C's own open-Q
6 asks whether collapsing domain (firewall) and confidence (trivial) into one
op-type hides risk. *Options:* (i) keep `set_field` as the super-op including
`domain` (fewer kinds, but the firewall-critical case is buried inside a generic
op and an allowlist-by-field must be perfect); (ii) split `domain` (and the
firewall-critical `relink_*` already separate) out of `set_field` into a dedicated
`move_domain` op so the capability allowlist gates an *op-type*, not a *field
inside an op*. *Provisional pick:* **(ii) — `domain` is NOT a `set_field` field;
it is the dedicated `move_domain` op (downgrade gated per (f)).** F's op-allowlist
is by `op_kind`; burying the highest-risk action as a discriminator value inside a
permitted op makes the allowlist a field-level check that is far easier to get
wrong. `set_field` keeps only intra-domain-safe fields. *What would flip it:* if a
field-level allowlist is shown to be as robust as an op-type allowlist (it must
re-derive domain and re-check RLS regardless), the fewer-kinds win could justify
re-merging — but the burden of proof is on the merge.

**Additional forks surfaced (lower-severity, for completeness):**
- **(h) merge-intent in model output (A R2).** Should the model be *forbidden* any
  `slot.merge` but `assert`? Provisional: **yes** — `add/remove/replace` are
  integrator-with-cue or human only (A §6, D C3). Flip: if eval shows the model
  reliably and safely emits `add` for explicit "also" cues.
- **(i) functional-over-time vs functional-now (B open-Q 1, G §4).** Is
  `currentEmployer` a functional predicate with temporal supersession, or a set
  filtered to "live now"? Provisional: **functional with a temporal succession of
  supersessions** (B/G lean). Flip: if "former employer" history comes out
  malformed under supersession.
- **(j) ambiguous-cardinality default (E open-Q 2, D §6).** Default unknown
  predicates to `set` (additive is safe; silent-replace is the dangerous failure)
  vs `functional` (avoids near-duplicate pollution). Provisional: **`set`** (E
  §6.3). Flip: if set-default produces worse graph pollution than silent replace.
- **(k) `structured` value variant — closed shapes vs model-coined (A open-Q 7).**
  Provisional: **registry-declared closed set** (A leans closed). Flip: a real case
  needing an un-registered shape.
- **(l) multi-note corroboration (B open-Q 6).** Add a provenance row to an
  "immutable" assertion vs supersede with merged provenance. Provisional: **add a
  provenance row** (provenance is additive evidence; content unchanged). Flip: if
  the audit implication of mutating an immutable row's child table is unacceptable.

---

## 8. Invariant check (§4 of framing)

| Invariant | Status | Where satisfied / gap |
|---|---|---|
| **LLM-adapter only** | ✔ | Both extraction stages route through the adapter (D §6); committer is deterministic, no SDK in its path. |
| **Storage abstraction** | ✔ | The committer is the sole writer and goes through the storage abstraction (F §reconciliation); provenance cites note_id via the abstraction, never raw paths (B §3.4). |
| **RLS domain firewalls + isolation test per new table** | ✔ (provisional, conflict-gated) | F §2/§5: per-domain projections, no cross-domain FK, materialization-level enforcement, isolation tests for every new table. **Gap:** depends on resolving §7(a) (global-vs-projection) and §7(g) (`move_domain` as its own op); the cross-domain resolver (F open-Q 2) is an unbuilt privileged asset. |
| **Bitemporal (valid vs reported time distinct)** | ✔ | G §2.5 + B §6.4: `valid_*` (structured per endpoint, bound trichotomy) and `reported_at`/`tx_[from,to)` are independent first-class axes; backdated-note case E7 works. |
| **Audit & reversibility (reopen/undo, provenance to note/span)** | ✔ | Op-log + immutable audit + precomputed stored inverse (C §2.5, B §4, F §2.2); batch undo via `batch_id`; provenance mandatory at every stage. **Watch:** undo composition of structure+identity ops (C open-Q 8) and domain-move undo re-leak (§7(f)). |
| **Machine-written wiki (#7)** | ✔ with one flagged soft edge | Unanimous typed-op position (§4.4); committer machine-writes, wiki re-derives. **Soft edge:** `add_fact` human-originated content (§7(e)) — bounded by `human_assertion` provenance, not yet adversarially proven. |
| **Conventional Commits / branch+PR / CI-green / tests-with-code** | n/a here | Process constraints on the eventual build; the spec stays buildable (op-log, deterministic committer, testcontainers RLS tests all CI-shaped). |

**§5 success-criteria check (abbreviated):** every §2 wishlist item maps to a
typed op (C §2.4 × E §1.2 tables, cross-checked); override-vs-add is explicit and
ergonomic (cardinality-in-key + offered_ops + array UI); contract is
schema-constrained, validated by deterministic backstops, versioned, migratable
(D); RLS preserved and every edit reversible/audited; the kind-zoo collapses to
one parameterized card (E §4). The residual risk surface is entirely in §7 — which
is what the red-team attacks next.

---

*End of v0. The seams in §7 are deliberate. Tear them apart.*
