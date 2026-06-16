# Fact pipeline & review redesign — integrated spec v0

**Status:** SYNTHESIS draft for adversarial red-team (Phase 2). Integrates research
tracks A–G against `00-framing.md`. Not a concatenation: one model, one contract,
one op-log, with every cross-track seam made visible in §7.
**Inputs:** `10-research-A-fact-ir.md` … `10-research-G-temporal.md`, `00-framing.md`.
**Decision posture:** every position below is *provisional* and labeled where tracks
disagree; §7 is the crux for the red-team.

---

## 1. Overview & design spine

The redesign rests on five load-bearing commitments that every track independently
converged on (A §1, B §1, C §1, D §0, E §0, F §4.1, G §5). They are the spine; the
rest of the spec hangs off them.

1. **Model proposes / deterministic committer decides.** The LLM (extraction +
   integration hints) is a fallible *parser* that emits *structured intent*; it holds
   **no write capability**. A single privileged, deterministic **committer** (the
   "arbiter") validates every proposed mutation against the predicate registry, value
   shapes, temporal soundness, and firewall rules, then performs the write. This is
   simultaneously the reliability boundary (D), the audit chokepoint (C), and the
   prompt-injection defense (F's CaMeL-style dual-LLM split). **schema-valid ≠ correct**
   (D §0): constrained decoding guarantees parse + enum membership and nothing about
   grounding — the committer closes that gap deterministically.

2. **Append-only op-log is the single audit + undo + change-feed mechanism.** Every
   committed change — model-authored or human-authored — is one or more rows in
   `fact_op` plus the immutable `fact_assertion` inserts it caused (B §4, C §2.5, F §2.2).
   Nothing is updated in place. Reversibility = the inverse is *also* an op; the op-log
   *is* the undo stack. The op-log is itself domain-scoped (F): a health correction is
   invisible to a general-domain audit view.

3. **Cardinality lives in the identity key, not in application branches.** The framing's
   central bug — "add another silently replaces the head" — is, at storage, an
   identity-key collision (B §1.2). **Functional predicates exclude the value from the
   slot key** (a new value supersedes); **set-valued predicates include a `value_identity`
   component** (a new value is a peer). No write path ever has an
   `if functional then override else add` branch to get wrong; the same insert path yields
   override or add purely from the key. The registry's `functional` flag is the sole
   authority (C §3, D §C3); the row snapshots it at write time so a later registry flip
   cannot silently re-interpret old rows (B §7-E).

4. **#7 (machine-written wiki) is preserved with no doctrine change** (A §6, B §6.1,
   C §4, E §5.2, F §6, G §7 — *unanimous*). #7 forbids humans *authoring graph/wiki state
   by hand*; it does **not** forbid humans *issuing typed correction operations the
   committer validates and applies*. A correction is a machine-applied mutation: the human
   supplies intent + arguments; the machine decides legality, writes, and emits audit +
   inverse. Compliance holds iff four conditions (C §4.2): no direct write; closed typed
   vocabulary; audited + reversible; the wiki still regenerates from facts. The **single
   soft edge** is `add_fact` (a human-originated fact the extractor never produced) —
   admitted only with `provenance.kind = "human_assertion"` citing the op as source,
   flagged and visibly attributed (C §4.4). This is the one spot the red-team should press.

5. **Bitemporal, typed, span-anchored facts.** Valid-time (when true in the world) and
   reported/transaction-time (when captured/recorded) are independent and both first-class
   (B §6.4, G §1). A value is **never a sentence** — it is one of a closed set of typed
   variants (A §2.3), which is *also* a security property (F §4.2: a typed enum can't carry
   "SYSTEM: …"). Every fact carries a verified source span.

**The pipeline, end to end:**

```
note → STAGE-1 extract (constrained decode: candidate facts, span-anchored, verbatim)
     → STAGE-2 type+link (constrained decode, per candidate, registry slice injected)
     → DETERMINISTIC validate→repair→backfill→gate  (sole authority; never silently
       wrong: terminal states are validated-commit OR review-item)
     → FactClaim (resolved) → committer applies fact_ops → fact_assertion (append-only)
     → review surfaces FactClaim projection; human emits fact_ops; same committer path
     → wiki regenerates from the fact graph (machine-written)
```

---

## 2. The fact contract — the `FactClaim` envelope

**Position (A §1, provisional):** one canonical envelope — the `FactClaim` — flows
**monotonically enriched** through every stage; stages differ only by which optional
sub-objects are populated, governed by a single `resolution` enum, not by distinct
schemas. *Seam:* B §6.2 dissents — storage takes a **narrower, stricter** persisted shape
with an explicit one-way mapping. Reconciled in §3/§7(a): **the IR is one monotone
envelope; the storage row is a projection of its resolved form.** Born at extraction with
mention-level refs and no IDs (`resolution: "mention"`); integration enriches the *same*
object in place; review sees it at `resolved`/`held`; edits are *ops against* it, never a
fourth shape.

### 2.1 Envelope

```jsonc
{
  "schema": "factclaim/1",           // mandatory contract version; pure up-migrations at the boundary
  "claim_id": "fc_01HZ...",          // ULID, minted at extraction, stable across enrichment
  "resolution": "mention",           // mention → resolved → held → committed (MONOTONE; reopen only via op)
  "split_group": null,               // shared id when one sentence split into N claims at extraction

  "subject": { /* Ref §2.3 */ },
  "predicate": { /* Predicate §2.4 */ },
  "value": { /* TypedValue §2.2 — a literal OR an edge; NEVER both */ },
  "slot": { /* Slot §2.4 — cardinality + merge intent */ },

  "modality": "asserted",            // asserted|negated|hypothetical|reported|question|expected
  "kind": "attribute",               // event|measurement|state|attribute|preference|relationship
  "domain": "general",               // general|health|finance|location (closed enum; committer re-derives)

  "confidence": 0.82,                // model emits; validator clamps + recalibrates; null allowed
  "provenance": { /* §2.5 — mandatory at EVERY stage */ },
  "temporal": { /* §2.6 — Track G owns shape */ },
  "notes": null                      // free-text rationale the model MAY emit; NEVER the value
}
```

`resolution` is monotone (A §2.1): validators key required-fields off it (at `mention`,
`entity_id` MUST be null + `mention` present; at `resolved`, `subject.entity_id` non-null,
object too iff `kind == relationship`). Backward moves require an explicit `reopen` op.

**Cross-field invariant (A R5 — the single most important consistency rule):**
`value.type == "ref"` ⟺ `kind == "relationship"` ⟺ object entity link exists. Checked
deterministically; divergence is rejected.

### 2.2 `TypedValue` — the 7-variant discriminated union

```jsonc
{ "type": "enum",     "code": "married", "label": "Married" }
{ "type": "quantity", "value": 5.4, "unit": "mmol/L", "precision": 0.1 }   // UCUM-style unit
{ "type": "date",     "value": "1984-03-12", "grain": "day" }              // a date LITERAL, distinct from temporal validity
{ "type": "boolean",  "value": true }
{ "type": "text",     "value": "anaphylaxis", "lang": "en" }               // ONLY free-text variant; bounded (≤120 chars unless value_shape=text)
{ "type": "structured", "shape": "address", "fields": { "line1": "12 Elm St", "city": "Austin", "region": "TX", "postal": "78701" } }
{ "type": "ref",      "ref": { /* a full Ref, §2.3 */ }, "role": "employer" }  // THE relationship case; carries NO scalar value
```

**Hard rules (A §2.3):** `type: ref` carries no scalar `value`; the six literal variants
carry no `ref`. The seven variants map 1:1 onto the registry's `value_shape` enum so
contract and registry cannot drift. The registry's declared `value_shape` is *expected*;
mismatch routes to a shape-mismatch review (never silent drop). The validator rejects a
`text` value over the bound for any predicate whose `value_shape ≠ text` — the deterministic
backstop that kills "value = whole sentence." **Provisional typing-authority position (§7c):
the model emits `type` only as a hint; the deterministic validator re-derives the typed
value from the cited span and is authoritative** (D §B2).

### 2.3 `Ref` — entity reference, mention retained + resolved id

```jsonc
// Pre-resolution (extraction emits):
{ "mention": { "surface": "Sam", "span": { "start": 41, "end": 44 }, "kind_hint": "person" },
  "entity_id": null, "candidate_ids": [] }

// Post-resolution (integration fills id; MENTION IS RETAINED — both coexist):
{ "mention": { "surface": "Sam", "span": { "start": 41, "end": 44 }, "kind_hint": "person" },
  "entity_id": "ent_7f3a", "candidate_ids": ["ent_7f3a", "ent_9b1c"] }

// Mint-new intent:
{ "mention": { "surface": "Dr. Okafor", "span": {"start":10,"end":20}, "kind_hint": "person" },
  "entity_id": null, "mint": { "kind": "person", "reason": "no candidate above threshold" } }
```

Mention retained forever (A §2.2): re-resolution ("which Sam"), audit, identity ops all
need the original surface. **Security note (F §2.3 / §7a):** at the *storage* layer
`entity_id` resolves to a **same-domain entity projection**, never a cross-domain global
row — see §3 for the B-vs-F seam.

### 2.4 `Predicate` + `Slot` (cardinality + merge intent in-flight)

```jsonc
"predicate": { "raw": "worksFor", "canonical": "person.employer",   // canonical filled by integrator; null at extraction
               "qualifier": { "audience": "close_friends" }, "value_shape": "ref" }
"slot": { "cardinality": "set",          // functional | set — FROM REGISTRY, never the model
          "merge": "add",                // assert | add | remove | replace
          "slot_key": "person.employer" }
```

`merge` verbs (A §2.4): `assert` (safe default — integrator decides supersede-vs-accumulate
from cardinality); `add` (append a set member); `remove` (retract one member); `replace`
(supersede the head). **Provisional guard (A R2 / §7):** the *model* may emit only `assert`;
`add`/`remove`/`replace` require an explicit re-validated cue or a human edit.

### 2.5 Provenance (mandatory, typed, every stage)

```jsonc
"provenance": {
  "note_id": "note_abc", "chunk_id": "chunk_3",
  "span": { "start": 18, "end": 67 },          // the SENTENCE span supporting the whole claim
  "extractor": "factclaim/1@grok",             // process-provenance 4-tuple (D §4.1):
  "prompt_version": "v3", "validator_version": "v3", "model_id": "...",
  "captured_at": "2026-06-16T14:00:00Z",       // reported-time anchor
  "kind": "extracted"                          // extracted | human_correction | human_assertion | inferred | agent
}
```

### 2.6 `temporal` object (Track G — bound trichotomy, precision-per-endpoint, rrule)

```jsonc
"temporal": {
  "schema_version": "g-temporal/1",
  "valid_from": { "instant": "2019-09", "precision": "month", "certainty": "asserted", "bound": "closed" },
  "valid_to":   { "instant": null,      "precision": "unknown", "certainty": "asserted", "bound": "open" },
  "status": "ongoing",                  // DERIVED: ongoing|current|ended|former|scheduled|recurring|unknown
  "status_reason": "valid_to.bound=open && valid_from<=now",
  "recurrence": null
}
```

**Bound trichotomy (G §2.2 — the anti-fabrication core):** `closed` (endpoint known),
`open` (no endpoint exists — "ongoing"), `unknown` (endpoint exists but value unknown —
"former without a date"). `unknown` end ⇒ `instant: null`, status `former`, **excluded from
current-value, rendered as a word, never a fabricated date or `— → 2026` glyph** (G §3).
Precision is per-endpoint (`instant|day|month|year|decade|era|unknown`), stored at known
granularity, never padded. `status` is derived, cached, always re-derivable (G C3).

**Recurrence (G §2.3, RFC-5545, lazy):**
```jsonc
"recurrence": { "rrule": "FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=2026-12-31", "dtstart": "2026-01-06",
                "rdates": ["2026-07-04"], "exdates": ["2026-09-08"],
                "overrides": [ { "recurrence_id": "2026-03-17",
                                 "patch": { "valid_from": { "instant": "2026-03-18", "precision": "day", "bound": "closed" } } } ],
                "tz": "America/Los_Angeles", "count_cap": 730 }
```
Realized set = `(expand(rrule,dtstart,window) ∪ rdates) − exdates`, then overrides applied.
Never materialized as N rows.

### 2.7 CONCRETE consolidated examples

**(a) Typed scalar — "my A1c was 5.4":**
```jsonc
{ "schema":"factclaim/1","resolution":"resolved","claim_id":"fc_a1c",
  "subject":{"mention":{"surface":"my","span":{"start":0,"end":2}},"entity_id":"ent_self"},
  "predicate":{"raw":"A1c","canonical":"health.a1c","value_shape":"quantity"},
  "value":{"type":"quantity","value":5.4,"unit":"%"},
  "slot":{"cardinality":"set","merge":"add","slot_key":"health.a1c"},
  "kind":"measurement","domain":"health","modality":"asserted",
  "temporal":{"valid_from":{"instant":"2026-06","precision":"month","bound":"closed"},
              "valid_to":{"instant":null,"precision":"instant","bound":"closed"},"status":"ended"},
  "provenance":{"note_id":"n1","span":{"start":0,"end":18},"kind":"extracted"} }
// validator REJECTS value:{"type":"text","value":"my A1c was 5.4"} for this predicate.
```

**(b) Relationship link — "Sam works for Acme":**
```jsonc
{ "resolution":"resolved","claim_id":"fc_emp",
  "subject":{"mention":{"surface":"Sam","span":{"start":0,"end":3}},"entity_id":"ent_sam"},
  "predicate":{"raw":"worksFor","canonical":"person.employer","value_shape":"ref"},
  "value":{"type":"ref","ref":{"mention":{"surface":"Acme","span":{"start":14,"end":18}},"entity_id":"ent_acme"},"role":"employer"},
  "slot":{"cardinality":"set","merge":"assert","slot_key":"person.employer"},
  "kind":"relationship","domain":"general","modality":"asserted",
  "temporal":{"valid_from":{"instant":"2019-09","precision":"month","bound":"closed"},
              "valid_to":{"instant":null,"bound":"open"},"status":"ongoing"},
  "provenance":{"note_id":"n2","span":{"start":0,"end":30},"kind":"extracted"} }
```

**(c) Multi-valued — "my daughters Summer, Harmony, Lydian" (one-claim-per-value + merge verb):**
```jsonc
// THREE claims sharing split_group; each its own mention span; merge:"add" ⇒ accumulate, never replace.
{ "claim_id":"fc_a","split_group":"sg_1","subject":{"entity_id":"ent_self"},
  "predicate":{"canonical":"person.child","value_shape":"ref"},
  "value":{"type":"ref","ref":{"mention":{"surface":"Summer","span":{"start":14,"end":20}},"entity_id":"ent_summer"},"role":"child"},
  "slot":{"cardinality":"set","merge":"add","slot_key":"person.child"},
  "kind":"relationship","modality":"asserted","domain":"general",
  "provenance":{"note_id":"n3","span":{"start":0,"end":40},"kind":"extracted"} }
// fc_b (Harmony, span 22–29, ent_harmony), fc_c (Lydian, span 34–40, ent_lydian): identical but for surface/span/object.
```

**(d) Negation / hypothetical:**
```jsonc
// "Sam is NOT allergic to penicillin" — modality on the ASSERTION; value types normally.
{ "claim_id":"fc_neg","subject":{"entity_id":"ent_sam"},
  "predicate":{"canonical":"health.allergy","value_shape":"text"},
  "value":{"type":"text","value":"penicillin"},
  "slot":{"cardinality":"set","merge":"assert"},
  "kind":"state","domain":"health","modality":"negated",
  "provenance":{"note_id":"n4","span":{"start":0,"end":34},"kind":"extracted"} }
// "if I switch to Acme next year" — modality:"hypothetical", future valid_from, low confidence.
// Carried into integration so it can later be confirmed; does NOT assert into the live graph floor until promoted.
{ "claim_id":"fc_hyp","subject":{"entity_id":"ent_self"},
  "predicate":{"canonical":"person.employer","value_shape":"ref"},
  "value":{"type":"ref","ref":{"mention":{"surface":"Acme"},"entity_id":"ent_acme"},"role":"employer"},
  "slot":{"cardinality":"set","merge":"assert"},"kind":"relationship","modality":"hypothetical",
  "confidence":0.3,"domain":"general",
  "temporal":{"valid_from":{"instant":"2027","precision":"year","bound":"closed"},"status":"scheduled"},
  "provenance":{"note_id":"n5","span":{"start":0,"end":28},"kind":"extracted"} }
```

**(e) Recurring — "PT every Tue/Thu through Dec 2026":**
```jsonc
{ "claim_id":"fc_pt","subject":{"entity_id":"ent_self"},
  "predicate":{"canonical":"health.therapy_session","value_shape":"text"},
  "value":{"type":"text","value":"physical therapy"},
  "slot":{"cardinality":"set","merge":"assert"},"kind":"event","domain":"health","modality":"asserted",
  "temporal":{"valid_from":{"instant":"2026-01-06","precision":"day","bound":"closed"},
              "valid_to":{"instant":"2026-12-31","precision":"day","bound":"closed"},"status":"recurring",
              "recurrence":{"rrule":"FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=2026-12-31","dtstart":"2026-01-06",
                            "exdates":["2026-09-08"],"tz":"America/Los_Angeles","count_cap":730}},
  "provenance":{"note_id":"n6","span":{"start":0,"end":44},"kind":"extracted"} }
```

**(f) Former-without-date — "used to work at Acme":**
```jsonc
{ "claim_id":"fc_former","subject":{"entity_id":"ent_self"},
  "predicate":{"canonical":"person.employer","value_shape":"ref"},
  "value":{"type":"ref","ref":{"mention":{"surface":"Acme"},"entity_id":"ent_acme"},"role":"employer"},
  "slot":{"cardinality":"set","merge":"assert"},"kind":"relationship","domain":"general","modality":"asserted",
  "temporal":{"valid_from":{"instant":"2019","precision":"year","bound":"closed"},
              "valid_to":{"instant":null,"precision":"unknown","certainty":"asserted","bound":"unknown"},  // CLOSED interval, UNKNOWN endpoint
              "status":"former"},
  "provenance":{"note_id":"n7","span":{"start":0,"end":24},"kind":"extracted"} }
// status=former ⇒ excluded from current-value; renders "former (since 2019)"; no fabricated end date.
```

---

## 3. Storage & graph model

### 3.1 Three layers (B §1.1)

- **Entity node** — a stable surrogate; identity is *resolved* (alias/embedding cluster +
  distinctness), not intrinsic. Split/merge operate here, O(1), via `redirect_to`.
- **Fact assertion** — the immutable, append-only edge row. The audit grain and unit of
  reversibility. **Never updated in place.**
- **Fact slot** — the *logical* fact: the identity key grouping assertions that are "the
  same fact over time." Derived (a `slot_key` column + partial live-index), not a stored
  table by default (materialization is an optional cache, never authoritative).

```sql
CREATE TABLE fact_assertion (
  assertion_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id       uuid NOT NULL,
  slot_key       bytea NOT NULL,            -- identity key (§3.2), derived at write
  value_identity bytea,                     -- set-member sub-identity; NULL for functional
  supersedes     uuid REFERENCES fact_assertion(assertion_id),
  op_id          uuid NOT NULL,             -- the fact_op that created this row
  subject_id     uuid NOT NULL,             -- → same-domain entity projection (see §3.5 seam)
  predicate      text NOT NULL,
  qualifier      text,                      -- participates in slot_key
  value_json     jsonb,
  object_id      uuid,                       -- set iff value_shape=ref
  predicate_kind text NOT NULL,
  cardinality    text NOT NULL,             -- SNAPSHOT of registry at write (B §7-E)
  modality       text NOT NULL DEFAULT 'asserted',
  domain_code    text NOT NULL,             -- firewall band; participates in slot_key
  confidence     real, pinned boolean NOT NULL DEFAULT false,
  -- BITEMPORAL (Track G owns valid-* precision/bound/recurrence; see §2.6 column slice):
  valid_from_instant timestamptz, valid_from_precision text, valid_from_bound text,
  valid_to_instant   timestamptz, valid_to_precision   text, valid_to_bound   text,
  recurrence     jsonb,
  tx_from        timestamptz NOT NULL DEFAULT now(),
  tx_to          timestamptz,               -- NULL = currently believed; set on supersede
  reported_at    timestamptz,               -- the SOURCE's claim time (≠ valid, ≠ tx)
  state          text NOT NULL DEFAULT 'live'  -- live|superseded|retracted|tombstone
);
CREATE UNIQUE INDEX one_live_per_slot ON fact_assertion (slot_key)
  WHERE tx_to IS NULL AND state = 'live';   -- set preds: slot_key includes value_identity ⇒ ≤1 live PER MEMBER
```

### 3.2 The slot key (the override-vs-array fix, made mechanical)

`slot_key = hash(owner_id, subject_id, predicate, COALESCE(qualifier,''), domain_code
[, value_identity IF cardinality='set'])`

- **Functional** ⇒ value excluded ⇒ new value with same key **supersedes**.
- **Set-valued** ⇒ `value_identity` included ⇒ a new value is a **peer**.
- **`value_identity`** (B §3.2) priority: object_id for `ref`; natural key (E.164 phone,
  lowercased email); else a minted member-id carried forward by every supersession. So a
  *typo fix* on a member supersedes that member (same `value_identity`) rather than forking
  an add. `add` mints; `replace` reuses; `remove` tombstones that `value_identity`.
- **Qualifier + domain participate** ⇒ a firewall move is a genuine re-key (new slot), not
  an in-place mutation (§6).

### 3.3 Bitemporal live-row selection — two independent gates (B §3.3, G §4)

`tx_to IS NULL` ("we still believe this record") AND the valid-time window
("true at the queried instant", respecting the bound trichotomy: `unknown` end ⇒ excluded
from current-value). Answers "what did we believe on D about where Sam worked in 2019" by
gating tx-time on D and valid-time on 2019. `is_current` is **derived, never a stored flag**
(G C3). Functional-over-time (e.g. `currentEmployer` history) is provisionally **functional
with a temporal succession of supersessions**, not a set filtered to "live now" (B §9.1 open).

### 3.4 `fact_op` log (B §4, F §2.2)

```sql
CREATE TABLE fact_op (
  op_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL,
  domain_id int NOT NULL,                  -- domain the op acts WITHIN (F)
  op_kind text NOT NULL,                   -- the ~22-op algebra (§4)
  actor text NOT NULL,                     -- 'human:<id>' | 'agent' | 'reprocess' | 'extractor'
  source text NOT NULL,                    -- capability boundary for the allowlist (F §4.2)
  target_slot bytea, payload jsonb NOT NULL,
  inverse_of uuid REFERENCES fact_op(op_id),  -- undo is itself an op
  batch_id uuid,                           -- a review session = atomic, jointly-undoable group
  applied boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);
```
One transaction per op (op row + its assertion inserts commit together or not at all,
B R7). A pinned live row cannot be superseded by `actor='reprocess'` (only human/agent) —
satisfies "reprocessing can't drop an approved fact" at the storage layer.

### 3.5 RECONCILED SEAM — global entity + redirect (B) vs per-domain projections (F)

The spec's deepest tension; both are presented, with a provisional position (full treatment
§7a).

- **Track B:** one global `entity` table; `subject_id`/`object_id` point at it; split/merge
  = flip `status` + `redirect_to`; reads follow redirects. O(1), trivially reversible.
- **Track F:** a *global* entity row referenced by facts of any domain is the dangerous
  shape — it enables (i) the **FK covert channel** (Postgres FK/unique/PK checks bypass RLS
  by design, leaking existence across the firewall) and (ii) a **cross-firewall read oracle**
  (a general `worksFor` edge dereferencing an object that also carries health attributes).
  F replaces it with **per-domain `entity_projection`** rows (one per `(canonical entity,
  domain)`, holding only that domain's name/attrs) joined by an access-controlled
  `entity_identity(canonical_id, projection_id, domain_id)`. Facts reference a **same-domain
  projection**, so no FK ever crosses a firewall; relink can only choose among in-scope
  projections; cross-domain identity is an `identity_merge`-class op, never a relink side
  effect.

**Provisional position:** adopt **F's per-domain projection as the persisted entity model**,
and apply **B's redirect/`canonical_id` mechanism *within* the canonical-id layer** —
`canonical_id` is B's stable surrogate, `redirect_to` operates on canonical ids, split/merge
stay O(1) and reversible, but facts never FK a cross-domain row. Keeps B's audit/reversibility
wins and F's firewall guarantees; the cost is identity-resolution complexity (the integrator
must reconcile `canonical_id` across domains *without* leaking — F §6.1/§6.2, escalated to
§7a). The all-`general` corpus pays little; the multi-domain case pays the
projection-multiplication cost.

---

## 4. Correction algebra & review submission

### 4.1 The ~22-op closed algebra (C §2.3)

Typed, named, intent-bearing operations — **not** JSON-Patch (semantically blind to
functional-vs-set, reintroduces the core ambiguity) and **not** corrected-full-record
(loses intent, can't express set-add-vs-replace or split/merge/identity). Each op is a pure
function `op : GraphState → (GraphState, AuditRecord, InverseOp)`; the inverse is itself a
member of the algebra; `preconditions` (the RFC-6902 `test` discipline) capture prior state
for invertibility + optimistic concurrency.

| group | ops | wishlist |
|---|---|---|
| A · per-field | `set_field`{predicate,qualifier,value*,modality,kind,confidence}, `retime` | 1,2,5,6,8 |
| (firewall)    | `domain_move` (hoisted out of `set_field` — see §7g)                  | 7 |
| B · entity-link | `relink_subject`, `relink_object`, `mint_and_link_object`, `unlink_object` | 3,4 |
| C · cardinality | `add_to_set`, `replace_head`, `remove_from_set` | 9 |
| D · structure | `split_fact`, `merge_facts`, `add_fact` | 10,11 |
| E · lifecycle | `retract`/`unretract`, `supersede`, `pin`/`unpin`, `set_confidence`, `fix_provenance` | 13,14,15 |
| F · identity | `merge_entities`/`unmerge_entities`, `split_entity`, `assert_distinct` | 12 |

`set_field` is the "fewer kinds" super-op (predicate/qualifier/modality/kind/confidence under
a `field` discriminator); `retime`, `relink_*`, the cardinality ops, and **`domain_move`**
stay separate because they carry distinct preconditions/firewall logic (§7g resolves C's
original inclusion of `domain` in `set_field` toward F's separate `domain_move`). The
G-temporal ops (`set_bound`, `clear_bound`, `set_precision`, `mark_former`, `mark_ongoing`,
`set_recurrence`, `add_exception`, `override_occurrence`, `correct_reported_time`) are the
**temporal subset of `retime`** and enforce S1–S6 soundness (esp. S2: `mark_former` with no
date ⇒ `bound=unknown`, never "now"; S4: valid-time edit never rewrites a prior tx version).

### 4.2 Functional-vs-set rule (C §3) — `offered_ops` is arbiter-authoritative

| predicate `functional` | legal value-ops | illegal (UI must not offer; arbiter rejects) |
|---|---|---|
| `true` | `set_field value`, `supersede` | `add_to_set`, `replace_head`, `remove_from_set` |
| `false` | `add_to_set`, `replace_head`, `remove_from_set` | `set_field value` (hard error) |

A human literally cannot `add_to_set` on `birthDate` or `set_field value` on `employer`.
`offered_ops` is computed by the **arbiter from the registry, never the client** — a hostile
or mis-rendered client cannot smuggle an illegal op. Changing a predicate's cardinality
within `set_field predicate` reconciles the value in the *same* op (functional→set wraps the
lone value as first member; set→functional with >1 live member is **rejected** — "remove or
merge first"); no transient ambiguous state. **Default for genuinely-ambiguous predicates:
set** (additive is safe; silent-replace is the dangerous failure — E §6.3).

### 4.3 Review submission — fat read / thin write (C + E reconciled)

Two asymmetric contracts (E §2): a **fat read projection** (server → card) carrying
everything the card needs to render editors without round-trips (predicate metadata +
`cardinality` + candidates + enum domains + `ui_capabilities` firewall gates), and a **thin
write** (card → server) = **one verdict + an ordered, typed op-list** (Track C's algebra),
ops referencing stable ids (`fact_id`/`value_id`/`entity_id`), `base_version` as the
optimistic-concurrency token. Ordering is explicit: field ops → structure ops → identity
ops, one transaction. `approve_with_edits` is the common path; `reject` carries a
`reason_code` and auto-drafts a correction note (doctrine escape hatch retained as a strict
subset).

**RECONCILED E-vs-A/B seam (§7b):** E presents a set-valued fact as **one record with N
temporally-scoped cells** (ergonomic for "Acme 2021–2023, now Beta" on one card); A/B store
**one assertion per member (one-edge-per-value)**. *Position:* the **review payload may
render cells; the submission and storage lower every cell to one-claim/one-assertion-per-
value** via `value_id`↔`value_identity`. The card is a view; the op-list and the slot_key
are the contract. Keeps add-vs-replace unambiguous end to end (B §6.3).

The kind-zoo collapses to **one card** parameterized by `(kind, value_shape, cardinality,
reason)` (E §4): appointments are `kind=event` surfaced by `reason=appointment_proposed`;
lab rows are `kind=measurement`/`value_shape=quantity` in `health`; conflicts are a `batch`;
split/merge/add-missing reuse the structure ops. New predicates/value-shapes extend the
**value-editor registry**, not the card zoo.

### 4.4 The #7 position (no doctrine change; `add_fact` carries human_assertion)

Per spine §1.4. The four compliance conditions (C §4.2) hold for all ops. **`add_fact`**
(wishlist 11) is the one soft edge: admitted only with `provenance.kind="human_assertion"`
citing the correction op as source, flagged in audit, visibly attributed wherever it
surfaces, and counted by a watch-metric. *Red-team crux (§7e):* `add_fact` vs forcing every
human-originated fact through a correction *note* that round-trips the extractor.

### 4.5 Audit record (C §2.5) — append-only, inverse precomputed at apply

One immutable `fact_audit` row per applied op: actor, `target_before`/`target_after`
snapshots, a fully-formed `inverse_op`, `graph_writes`, optional `correction_note_id`,
`undone_by` (stamped on undo; the row is never deleted). Replaying the op-log from genesis
reconstructs the graph (event-sourcing guarantee). **Pragmatic middle (C §5, provisional):**
the graph is the live store; the op-log + audit is the authoritative change history;
replay-from-genesis is required only for forensic reconstruction + undo, not normal reads.

---

## 5. Extraction & reliability (Track D)

### 5.1 Two-stage prompt (D §1.1)

- **Stage 1 — segment + extract** (constrained decode, "candidate fact" schema): emits
  **verbatim** surface phrases + cues + `source_span` offsets, *not* typed values or enums.
  Cheap, high-recall, span-anchored. One call batches a whole note.
- **Stage 2 — type + link** (constrained decode, per candidate): the **predicate-registry
  slice and entity candidates are injected**, turning the two worst hallucination surfaces
  (inventing a predicate / inventing an entity id) into *multiple choice*. Emits the rich
  `factclaim/1` draft. Few-shot (~8–12) curated to the *failure modes* (multi-valued split,
  typed quantity, date precision, negation, hypothetical, reported, existing-link, functional
  supersession cue, coined predicate, over-extraction trap) and kept in the golden set as
  regression anchors.

### 5.2 Deterministic validate → repair → backfill → gate (the SOLE authority, D §3)

Runs after every extraction; the only thing allowed to leave the stage. Two terminal states
only: **validated-commit** or **review-item** — never silently wrong, never silently dropped.

- **A · structural:** schema conformance (defence-in-depth even under constrained decode),
  required-field presence keyed off `resolution`, `contract_version` pin (reject stale shape).
- **B · grounding (anti-hallucination core):** **B1** span verification (`value.raw` is a
  fuzzy substring of `note[span]`; re-anchor slightly-off offsets; a value absent from the
  span → review, never commit); **B2** typed-value **re-derivation** (re-parse the typed
  value deterministically from `raw`; model's typed fields are *hints that lose ties to the
  parse* — §7c); **B3** modality/negation cue cross-check (lexicon disagreement ⇒ lower
  confidence + review, **never auto-flip**); **B4** provenance integrity.
- **C · vocabulary:** enum coercion; predicate canonicalization (embedding-assisted registry;
  coined slugs deduped against near-neighbours); **C3 cardinality stamping from the registry,
  not the model** — the deterministic answer to override-vs-array at the contract boundary.
- **D · link/firewall (100% tested):** entity existence *in current RLS scope*; firewall
  guard (cross-firewall link ⇒ review with consequence surfaced); candidate-rank consistency;
  self-link guard.
- **E · repair orchestration:** structured re-ask with validator errors appended, **capped
  N=2**, then **graceful degradation to a review item** (never commit a guess); every repair
  annotated (`repaired_by:[...]`), idempotent, pure.
- **F · calibration:** confidence clamp + recalibration (cap when any backstop fired);
  backstop-firing budget (>k fired ⇒ review).

### 5.3 Contract versioning + budgeted re-analysis migration (D §4)

Single pinned version string in schema/prompt/validator/every fact; each fact stamps the
4-tuple process-provenance. SemVer: *patch* (additive optional / tail enum — no re-extract);
*minor* (new required field with deterministic backfill — no model calls); *major* (shape
change needing the model — planned re-analysis). Re-analysis is a **first-class scheduled
workflow** on the Phase-5 engine + run-log: plan → **hard token/$ budget gate** → shadow +
deterministic per-field diff → cutover (flip the active-version pointer; old set retained for
audit/undo) → rollback by re-pointing. **Pinned facts are immutable to migration unless
explicitly reviewed** — re-analysis can never silently overwrite a human-approved fact.

### 5.4 Eval gates (D §5)

Frozen, human-adjudicated golden set (multi-valued, typed quantities/dates with precision,
every modality, existing-vs-mint links, ambiguous links, recurrence, **negatives** for
over-extraction), held-out split. Per-field **semantic** metrics via bipartite alignment
(not string-match): extraction P/R/F1, value-typing, link accuracy incl. *ambiguity-honesty*,
temporal at gold precision, modality confusion matrix, predicate, cardinality, backstop
efficacy, calibration (ECE). **Zero-tolerance gates: negated/hypothetical→asserted and
hallucinated-link counts.** CI runs hermetic via record/replay cassettes (LLM faked per repo
rule); backstop-ablation test proves the safety net earns its keep; **adversarial/jailbreak
slice** (prompt-injection notes must NOT produce cross-firewall links — ties to F); model-swap
eval is the gate for adopting any new model.

---

## 6. Security (Track F)

**Three rules bound the expanded attack surface to near-zero new firewall risk:**

1. **The privileged committer is the sole writer.** Neither the LLM nor the review UI writes
   `fact_assertion`/`entity_projection` directly (revoke direct DML from the app role; only
   the committer role applies ops). The committer **re-derives `domain_id` from the operands**
   (subject projection's domain + provenance note's domain) and **ignores any domain the
   model/payload claims**. RLS `WITH CHECK (domain_id = ANY(current_domains()))` is the second
   line. `SET LOCAL` GUC per transaction (never `SET`); unset scope ⇒ zero rows + write fails
   closed; `FORCE ROW LEVEL SECURITY`; no `BYPASSRLS` in the app path.

2. **Domain projections vs global entities** (the §3.5 / §7a seam): F's per-domain
   `entity_projection` + `entity_identity` is adopted as the persisted model — facts reference
   a **same-domain projection**, killing the FK covert channel (no cross-domain FK, ever —
   R4) and the relink read-oracle (relink chooses only among in-scope projections). Firewall
   is enforced at **value materialization, not row visibility alone** (R3): an edge may
   *reference* an out-of-scope canonical id but a session may never *render* its protected
   attributes.

3. **Domain-downgrade gating** (health/finance/location → general — the high-blast-radius
   direction): **(a) the LLM can NEVER emit a `domain_move` op** (absent from its op-type
   allowlist — attack 1's "move all of Dad's facts to general" is structurally
   unrepresentable); **(b) owner-only** (`source='human:owner'`; committer rejects any other
   principal); **(c) explicit, non-batchable, non-pre-filled confirmation** showing exactly
   which values + provenance become visible (defeats owner-fatigue social-engineering);
   **(d) copy-forward, not relabel** — a *new* general fact + general projection is created,
   citing the original as provenance, the original marked `superseded`, audit written in
   **both** domains, original never destroyed (reversible); **(e) bounded** — one fact per op,
   rate-limited, no wildcard, cascade explicit (moving a fact never auto-moves its object or
   siblings). Asymmetry: *upgrade* (general → protected, raising a wall) is lower-risk; the
   gates target the *downgrade*.

**Op-type allowlist by source** (the capability boundary): `extractor` may emit
`{set_field, add_to_set, relink(in-scope only), retime, …}` but **never**
`{domain_move, identity_merge-across-domains, pin, retract-of-pinned}`. A human edit and a
model proposal traverse the *identical* committer + RLS path; the editable surface is wide
but the *commit* surface is the same narrow chokepoint. Typed values shrink the injection
channel (an enum/quantity/date can't carry "SYSTEM: …").

**Isolation-test obligations (per new table, real Postgres via testcontainers, two scoped
sessions `S_general`/`S_health`):** read-isolation (zero cross-domain rows); `WITH CHECK`
write-rejection; unset-scope sees zero / writes nothing; `FORCE` polices the owner role; **no
SQL FK from any general row to a protected row** (schema-introspection test); cross-firewall
relink rejected; general edge exposes only the general projection; `entity_identity` can't be
enumerated cross-domain; injected-op corpus yields no `domain_move`/out-of-scope relink and
committer-derived domain wins over model-claimed; downgrade is copy-forward + dual-domain
audit + reversible; every applied op has exactly one append-only audit row; `reverses_op`
round-trips. Every new table ships its RLS isolation test in the same PR (CLAUDE.md rule 3).

---

## 7. OPEN CONFLICTS & DECISIONS (the crux for the red-team)

**(a) Global entity + redirect (B) vs per-domain entity projections (F).**
*Conflict:* B persists one global `entity` table (facts FK it; split/merge via `redirect_to`,
O(1), reversible). F argues a global row referenced cross-domain is the firewall's worst
shape — it enables the Postgres FK covert channel and a relink read-oracle — and replaces it
with same-domain `entity_projection` + an access-controlled `entity_identity`.
*Options:* (1) B's global table + RLS on attribute reads; (2) F's per-domain projections; (3)
hybrid — projections persisted, B's `redirect_to`/`canonical_id` operating on canonical ids.
*Provisional pick:* **(3) hybrid** — F's projection model for persistence + storage isolation,
B's redirect mechanics lifted to the canonical-id layer for O(1) reversible split/merge.
*Why:* the only option that keeps **both** F's firewall guarantees (no cross-domain FK,
materialization-level enforcement) **and** B's audit/reversibility wins.
*What would change it:* if the integrator cannot reconcile `canonical_id` across domains
without a privileged step that itself becomes a cross-domain oracle (F §6.1/§6.2), or if
projection-multiplication blows the identity-resolution budget, fall back to (1) with
attribute-level RLS and accept the FK-channel mitigation cost.

**(b) One-claim-per-value (A/B) vs one-record-N-cells review view (E).**
*Conflict:* A/B require one assertion per set member (per-value provenance/modality/remove
atomic; add-vs-replace unambiguous via `value_identity`). E wants the *card* to show one fact
with N temporally-scoped cells (ergonomic for job histories).
*Options:* (1) cells all the way down; (2) one-edge-per-value all the way up; (3) cells in the
read projection, one-edge-per-value in submission + storage.
*Provisional pick:* **(3)** — the payload may render cells; submission + storage lower every
cell to one-claim/one-assertion-per-value via `value_id`↔`value_identity`.
*Why:* B is categorical that arrays must lower or add-vs-replace breaks again; E's cell view
is a presentation that maps cleanly onto members.
*What would change it:* if split/merge + supersession serialize more cleanly from cells than
from edges (E §7.3), or if the cell→edge lowering proves lossy for per-cell temporal.

**(c) Value-typing authority: model-typed (A) vs deterministic span re-derivation (D).**
*Conflict:* A's envelope lets the producer set `value.type`; D insists the validator
re-derives the typed value from the cited span and the model's typing is only a hint that
*loses ties to the parser*.
*Options:* (1) model authoritative; (2) parser authoritative (model hints); (3) parser
authoritative *except* a flagged class where the model is more right (messy units, locale
dates).
*Provisional pick:* **(2)** — parser wins; model `type` is a hint; irreconcilable disagreement
⇒ review.
*Why:* "a value is never a sentence" needs a deterministic guarantee, and typed parse is
deterministic-friendly.
*What would change it:* evidence of a value class where the parser silently corrupts a value
the model got right and no test catches it (D §7.3) ⇒ carve out (3).

**(d) Span-anchoring (D) vs INFERRED facts' provenance.**
*Conflict:* D's B1 backstop requires `value.raw` be a substring of the cited span and rejects
otherwise — but genuinely *inferred* facts ("turned 40 last week" → birth year; "must have
been after X") are not quoted and would be rejected or fabricated.
*Options:* (1) reject all non-quoted values (loses real inferences); (2) an `inferred`
provenance flag exempt from B1 (re-opens the hallucination hole); (3) `inferred` allowed but
forced low-confidence + mandatory review + `certainty=inferred` on temporal, never
auto-committed.
*Provisional pick:* **(3)** — an `inferred` provenance kind (added to §2.5), B1-exempt but
routed to review, confidence-capped, attributed.
*Why:* the system must support derived facts (birth year from age) without re-opening
ungrounded auto-commit.
*What would change it:* if review volume from inferred facts floods the inbox, tighten to (1)
for low-value inferences; if inferred facts prove safe at high confidence, relax.

**(e) `add_fact` op (C) vs forced correction-note round-trip.**
*Conflict:* `add_fact` lets a human inject a fact the extractor never produced — human-
*originated* graph content, brushing #7's spirit. The alternative forces it through a
correction note that re-runs the extractor.
*Options:* (1) `add_fact` with mandatory `human_assertion` provenance + visible attribution;
(2) force a correction note; (3) `add_fact` but only after an extractor round-trip confirms it.
*Provisional pick:* **(1)** — direct `add_fact` with hard provenance flag + watch-metric.
*Why:* forcing a note for "the model literally missed my daughter's name" is poor ergonomics
(success §5: cognitive load down) and the provenance flag preserves auditability.
*What would change it:* if the human-asserted-fact metric climbs (doctrine erosion, C risk 3),
or if the red-team shows attribution is routinely stripped downstream, switch to (2)/(3).

**(f) Domain-move reversibility / laundering.**
*Conflict:* F makes downgrade a reversible `copy-forward`; C asks whether undo is *ever* truly
safe once a fact was read/cited across the boundary, and F §6.3 flags a move+undo+retime
sequence that could *launder* a value. C also asks whether domain-move should be one-way
(new-fact) rather than a reversible `set_field`.
*Options:* (1) reversible copy-forward (F's current); (2) one-way downgrade producing a new
fact, no reversible `set_field domain`; (3) reversible but with cite-tracking that flags
downstream cites for re-evaluation on undo.
*Provisional pick:* **(3)** — copy-forward downgrade, reversible, with the audit recording
downstream cites and undo flagging them for re-evaluation rather than silently breaking.
*Why:* keeps the §4 reversibility invariant while acknowledging C's "the cite may now dangle."
*What would change it:* if the red-team constructs a concrete move+undo+retime laundering
exploit that (3) doesn't catch, downgrade becomes one-way (2) and undo becomes a fresh
owner-authored re-upgrade, not a `reverses_op`.

**(g) `set_field` super-op hiding firewall risk.**
*Conflict:* C collapsed predicate/qualifier/modality/**domain**/kind/confidence into one
`set_field` op for the "fewer kinds" win — but `domain` is a firewall move and `confidence`
is trivial; folding them into one op-type hides the highest-risk edit behind a generic verb
(C §7.6), and F's allowlist *already* names `domain_move` as its own op-type — so C and F were
quietly inconsistent.
*Options:* (1) keep `set_field` unified, gate `domain` inside it; (2) split `domain_move`
(and identity ops) out as distinct, individually-audited op-types; (3) unified op-type but a
distinct *audit class* + allowlist entry for `set_field{domain}`.
*Provisional pick:* **(2)** — hoist `domain_move` out of `set_field` into its own op-type
(done in §4.1); `set_field` keeps only the genuinely-low-risk fields.
*Why:* the firewall control must be legible at the op-type granularity the allowlist and audit
filter on; this resolves the C/F inconsistency toward F.
*What would change it:* if (3)'s audit-class tagging proves sufficient for the allowlist and
isolation tests, keep the collapse for ergonomics.

---

## 8. Invariant check against framing §4

- **LLM-adapter only** — ✔ the model proposes via the adapter; the committer is the sole
  writer; constrained-decode calls route through the adapter (D §6, F reconciliation).
- **Storage abstraction** — ✔ committer goes through the storage abstraction; `note_id`/spans
  via the abstraction, never raw paths (B §3.4).
- **RLS domain firewalls** — ✔ strengthened: materialization-level enforcement + per-domain
  projections + no cross-domain FK + committer domain re-derivation + downgrade gating; every
  new table (`fact_assertion`, `fact_op`, `fact_audit`, `entity_projection`, `entity_identity`)
  ships an RLS isolation test (§6). *Residual:* the §7a hybrid's cross-domain `canonical_id`
  resolver is a new high-value asset (F §6.2) — red-team target.
- **Bitemporal model** — ✔ valid-time (bound trichotomy + precision-per-endpoint) and
  reported/transaction-time independent and first-class (§2.6, §3.3, G).
- **Audit & reversibility** — ✔ append-only op-log = audit + undo; every op has a precomputed
  inverse; batch undo is loss-free (closure is `tx_to`, not deletes). *Residual:* undo of
  composed structure+identity ops (C §7.8) and domain-move laundering (§7f) are open.
- **Machine-written wiki (#7)** — ✔ preserved with no doctrine change; the one soft edge
  (`add_fact` / human_assertion) is explicit, attributed, metered, and is §7e's crux.
- **Conventional Commits / branch+PR / CI-green / tests-with-code** — noted as process
  constraints on the eventual build; no code written in this effort (framing §8).

**Wishlist coverage:** all 15 §2 items map to ops (§4.1 table); override-vs-add is explicit
and ergonomic (§3.2 key + §4.2 `offered_ops`); the review kind-zoo collapses to one card +
sub-editors keyed on `(kind, value_shape, cardinality, reason)` (§4.3). Success criteria §5
met *provisionally*, pending the §7 conflicts surviving the red-team with no new Sev-1/2.
