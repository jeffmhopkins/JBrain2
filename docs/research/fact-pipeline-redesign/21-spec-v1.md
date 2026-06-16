# Fact pipeline & review redesign — integrated spec v1

**Status:** REVISION after Round-1 red-team. Folds every Sev-1 and Sev-2 finding from
the six R1 lenses (correctness, model, security, migration, ergonomics, performance)
into v0 — each FIXED, ACCEPTED-RISK, or DEFERRED with a stated reason (§9 disposition
table). Keeps v0's §1–§8 structure, revised; adds §9.
**Inputs:** `20-spec-v0.md`, `30-redteam-r1-{correctness,model,security,migration,ergonomics,performance}.md`, `00-framing.md`, `01-decisions-log.md` (**D1: complete DB reset acceptable → initial cutover is a clean rebuild; legacy in-place migration OUT OF SCOPE**; patched into §5.5 / §9-M7 post-authoring).
**Decision posture:** the §7 conflicts are now mostly *settled* (the red-team converged);
the few that remain open are marked and surfaced for the user.

---

## 1. Overview & design spine

Five load-bearing commitments survive R1; three are materially revised (spine #2 undo,
spine #3 cardinality, spine #5 modality). They are the spine; the rest hangs off them.

1. **Model proposes / deterministic committer decides.** The LLM is a fallible *parser*
   emitting *structured intent*; it holds **no write capability**. A single privileged,
   deterministic **committer** validates every proposed mutation against the predicate
   registry, value shapes (with a **plausibility-range + predicate/value co-location
   guard** — §5.B2b, new), temporal soundness, and firewall rules, then writes. This is
   the reliability boundary (D), the audit chokepoint (C), and the prompt-injection
   defense (F). **schema-valid ≠ correct ≠ grounded:** constrained decoding guarantees
   parse + enum membership; a *deterministic but prose-derived* backstop (span, raw, cue
   lexicon) is only *reproducible*, not *correct* — so every backstop whose oracle is the
   untrusted prose is paired with an **independent oracle** (registry range, closed typed
   inference verifier, co-location) (model-lens through-line).

2. **Append-only bitemporal store is the source of truth; undo is SNAPSHOT/state-based;
   the op-log is append-only history + immutable checkpoints — NOT per-op precomputed
   inverses.** (Revised: migration M1/M6/M8, ergonomics F2.) `fact_assertion` is
   append-only and already holds every prior state. **Undo = tombstone the assertions a
   target op wrote and un-tombstone the ones it superseded** (read off `op_id` +
   `supersedes`), gated by an **undo-dependency check** — undo of op *k* is legal only
   when no later live op depends on *k*'s outputs (same `slot_key`/`value_identity`/
   entity); otherwise it **cascades** (undo dependents first, shown as a preview) or is
   **blocked with an explicit dependency error**. Selective mid-history "remove k's delta
   but keep k+1" is a **new forward correction**, not an undo. This **deletes the ~22
   precomputed inverse definitions** and their migration ladder. **Genesis-replay is
   dropped as a guarantee** (ops are impure — they invoke versioned registry/resolver/
   parser); instead **every op freezes its resolved outputs + the pipeline version-tuple
   into the op**, so any replay re-applies *recorded outcomes*, never re-derives. Replay
   is forensic, to the nearest **immutable checkpoint** of the materialized graph (bounds
   cost; lets the log be archived behind a checkpoint, never truncated). The op-log
   remains the single audit/undo/change-feed mechanism; only the over-strong claims go.

3. **Cardinality lives in the identity key — with TWO keys, and modality in BOTH.**
   (Revised: correctness S1-1/S1-4, model SEV-2.1.) **Functional predicates exclude the
   value from the live-selection key** (new value supersedes); **set-valued predicates
   include a `value_identity`** (new value is a peer). The registry's `functional` flag
   is authority, **snapshotted at write** (a later flip can't re-interpret old rows). For
   **functional-over-time** the single value-excluding key is insufficient (§7(i)): a fact
   needs **two identities** — a *history/identity key that INCLUDES value* (so "all Acme
   stints" is one clean group + the supersession chain is walkable) and a *live-selection
   key that EXCLUDES value* (so "exactly one current" holds). **`modality` participates in
   BOTH keys**, and **non-asserted rows are never live-selected** (spine #5).

4. **#7 (machine-written wiki) preserved with no doctrine change.** Humans issue typed
   correction operations; the committer validates, writes, audits. The **one soft edge**
   `add_fact` is **hardened** (security S4, model crux §7e): it must cite a **real,
   in-scope provenance note/span OR round-trip the correction-note** — the "cite the op as
   source" escape is **removed** for any fact whose subject is freshly minted or whose
   domain isn't operand-derivable; attribution `human_assertion` is a **non-droppable,
   indexed** column distinguishable at every read surface; watch-metric counts it.

5. **Bitemporal, typed, span-anchored facts — and MODALITY is a first-class live-selection
   gate.** (Revised: correctness S1-1/S1-2/S2-8, S3-5.) Valid-time and reported/transaction
   time are independent and first-class. A value is **never a sentence** (also a security
   property: a typed enum can't carry "SYSTEM: …"). Every fact carries a verified span.
   **`current()` filters `modality='asserted'` by construction** — negated / hypothetical
   / reported / question / expected rows are **excluded from current-value** and live in a
   separate logical *candidate floor*; **promotion** (hypothetical→asserted on confirmation,
   expected→asserted on realization) is an explicit op, **never** an implicit consequence of
   `now` crossing `valid_from`. A negation can **never** overwrite an assertion (distinct
   slots that *conflict* → contradiction review, not silent supersession).

**The pipeline, end to end:**

```
note → STAGE-1 segment+extract (constrained decode: candidate clauses, verbatim, span-anchored;
       deterministic fact-bearing-clause precision gate before Stage 2)
     → STAGE-2 type+link (constrained decode, per candidate; note-level cached retrieval of
       predicate slice + entity candidates; truncation detected via finish_reason)
     → DETERMINISTIC validate→repair→backfill→gate (sole authority; range+co-location guard;
       modality/domain gate; closed inference verifiers; terminal: validated-commit OR review-item)
     → ResolvedFact (typed) → committer applies fact_ops → fact_assertion (append-only)
       + fact_current (materialized, authoritative-cache) updated in the SAME txn
     → review surfaces ResolvedFact projection (Approve / Needs-fix triage); human emits ops; same committer
     → wiki regenerates from the fact graph (machine-written)
```

---

## 2. The fact contract — honest typed stage shapes

**Position (revised: ergonomics F3, model SEV-1.4):** v0's "one fat monotone envelope"
is dropped as the *framing*; the spec already conceded three shapes (extraction, storage,
review) hidden behind optionality. v1 names them: **`ExtractedClaim` → `ResolvedFact` →
(projection) `ReviewCard`**, each transition one total function the compiler checks
(illegal-state-unrepresentable rather than prose-forbidden). The `claim_id` ULID is
carried as a *field* across transitions, not a shared mutable identity. Storage is a
strict projection of `ResolvedFact`. The wire shapes below are the same JSON as v0; only
the *typing discipline* changed (each stage is its own type, not one optional-everything
envelope).

### 2.1 `ExtractedClaim` (Stage-1/2 emit) and `ResolvedFact` (post-validate)

```jsonc
// ResolvedFact (committed shape; ExtractedClaim is the subset with entity_id=null,
// canonical=null, slot identity absent — see §2.4):
{
  "schema": "factclaim/2",           // contract version; pure up-migrations at the boundary
  "claim_id": "fc_01HZ...",          // ULID minted at extraction, carried as a field
  "stage": "resolved",               // extracted | resolved (NOT one mutable enum; the type is the stage)
  "split_group": null,               // shared id when one clause split into N claims
  "split_lineage": null,             // op_id of a human split_fact this claim must reconcile against (S2-1)

  "subject": { /* Ref §2.3 */ },
  "predicate": { /* Predicate §2.4 */ },
  "value": { /* TypedValue §2.2 — a literal OR an edge; NEVER both */ },
  "slot": { /* Slot §2.4 — cardinality + merge intent */ },

  "modality": "asserted",            // asserted|negated|hypothetical|reported|question|expected
                                     //   — GATES live-selection (§3.3) and participates in BOTH keys (§3.2)
  "kind": "attribute",               // event|measurement|state|attribute|preference|relationship — DISPLAY HINT (F1)
  "domain": null,                    // SCHEMA-ABSENT on the wire; committer DERIVES it (§6). Model/human MAY NOT set it.

  "confidence": 0.82,                // model emits; validator clamps + recalibrates; null allowed
  "provenance": { /* §2.5 — mandatory, real note/span (or closed-inference template) */ },
  "temporal": { /* §2.6 */ },
  "notes": null                      // model rationale; validator rejects if it echoes value.raw (model SEV-3.3)
}
```

**Cross-field invariant (bi-conditional, both directions tested — model SEV-1.4):**
`value.type == "ref"` ⟺ `kind == "relationship"` ⟺ object entity link exists. The
committer **re-derives `kind` from `value.type` + the registry's `value_shape`** rather
than trusting the model's `kind`; a literal value on a `ref`-shape predicate (or vice
versa) is a hard shape-mismatch review, never a commit (kills the dangling-edge case).

### 2.2 `TypedValue` — discriminated union (7 variants retained)

```jsonc
{ "type": "enum",     "code": "married", "label": "Married" }
{ "type": "quantity", "value": 5.4, "unit": "mmol/L", "precision": 0.1 }   // UCUM-style unit
{ "type": "date",     "value": "1984-03-12", "grain": "day" }
{ "type": "boolean",  "value": true }
{ "type": "text",     "value": "anaphylaxis", "lang": "en" }               // ≤120 chars unless value_shape=text
{ "type": "structured", "shape": "address", "fields": { "line1": "12 Elm St", "city": "Austin", "region": "TX", "postal": "78701" } }
{ "type": "ref",      "ref": { /* Ref §2.3 */ }, "role": "employer" }
```

**Hard rules:** `ref` carries no scalar; the six literals carry no `ref`. The seven map
1:1 onto the registry `value_shape` enum (contract↔registry can't drift). `structured`
shapes are a **HARD CLOSED SET** — an unknown `shape` is a shape-mismatch review with a
`propose_shape` fast-path, **never** model-coined (model SEV-2.3, correctness (k)).
*(`boolean` and a build-on-demand `structured` are an accepted nit, F7 — see §9.)*

**Typing authority (settled §7c): the deterministic parser is authoritative BUT guarded.**
The model emits `type` only as a hint; the parser re-derives the typed value from the
**verified-correct span**. Two new guards close the "parser+model agree on a wrong
value" hole (model SEV-1.1, correctness S2-5):
- **B2b registry plausibility-range gate:** every quantity/date is checked against the
  *fact's predicate* range stamped from the registry (A1c ∈ [3,20]%; glucose ∈ [40,600]
  mg/dL; child-count is small). In-range for the *wrong* predicate still fails the *fact's*
  predicate range → review.
- **Predicate/value co-location:** the value must sit in the **minimal clause** containing
  the predicate cue; a value merely "somewhere in a sentence with other numbers" fails.
- **Low-confidence within-variant parses** (locale decimal comma, ambiguous D/M/Y field
  order, magnitude-changing unit coercion) route to review even when parser and model
  *agree on the variant*. Locale/units are carried as explicit registry context so the
  parser isn't guessing.

### 2.3 `Ref` — entity reference (mention retained + resolved id)

```jsonc
// Pre-resolution: { "mention": {...}, "entity_id": null, "candidate_ids": [] }
// Post-resolution: { "mention": {...}, "entity_id": "ent_7f3a", "candidate_ids": [...] }
// Mint-new intent: { "mention": {...}, "entity_id": null, "mint": { "kind": "person", "reason": "..." } }
```

Mention retained forever. At storage, `entity_id` resolves to a **same-domain entity
projection**, never a cross-domain global row (§3.5). `candidate_ids` for a general fact
**contains only general projections** — never a cross-domain candidate signal (security
S1). Mint forced by a retrieval miss is **provisional + flagged** for a deferred dedup
pass, never a silent hard duplicate (model SEV-2.4).

### 2.4 `Predicate` + `Slot`

```jsonc
"predicate": { "raw": "worksFor", "canonical": "person.employer", "qualifier": {...}, "value_shape": "ref" }
"slot": { "cardinality": "set",   // functional | set — FROM REGISTRY, snapshotted; never the model
          "merge": "add",         // assert | add | remove | replace
          "slot_key": "person.employer" }
```

`merge` verbs: the **model may emit only `assert`**; `add`/`remove`/`replace` require a
human edit (kept — model (h)). **New guard (model SEV-2.1):** a Stage-1 *additive cue*
("also", "another", "second", a distinct qualifier like "work cell") on a predicate the
registry calls `functional` is a **registry-vs-evidence conflict → review**, never a
silent functional supersession that destroys a prior member. Low-confidence `functional`
flags default to `set` (the safe direction).

### 2.5 Provenance (mandatory, typed, real)

```jsonc
"provenance": {
  "note_id": "note_abc", "chunk_id": "chunk_3",
  "span": { "start": 18, "end": 67 },
  "extractor": "factclaim/2@grok", "prompt_version": "v3", "validator_version": "v3",
  "registry_version": "r12", "model_id": "...",          // FULL 4+1-tuple, frozen onto the OP too (§3.4)
  "captured_at": "2026-06-16T14:00:00Z",                 // reported-time anchor (single name; SEV-3.1 seam closed)
  "kind": "extracted",                                   // extracted | human_correction | human_assertion | inferred | agent | migrated
  "inference_template": null                             // set iff kind=inferred; one of the CLOSED set (§7d)
}
```

`human_assertion` is **non-droppable + indexed**; `migrated` carries low confidence and
flags the row for member re-anchoring (§5.5). `kind=inferred` is admissible **only** with
a closed-set `inference_template` whose deterministic verifier recomputed the value from
cited literal spans (§7d).

### 2.6 `temporal` (bound trichotomy; precision-per-endpoint; rrule)

```jsonc
"temporal": {
  "schema_version": "g-temporal/2",
  "valid_from": { "instant": "2019-09", "precision": "month", "certainty": "asserted", "bound": "closed" },
  "valid_to":   { "instant": null,      "precision": "unknown", "certainty": "asserted", "bound": "open" },
  "status": "ongoing",                  // DERIVED, cached, re-derivable
  "status_reason": "valid_to.bound=open && valid_from<=now",
  "recurrence": null
}
```

**Bound trichotomy:** `closed` (endpoint known), `open` (no endpoint — ongoing),
`unknown` (endpoint exists but value unknown — "former without a date"). `unknown` end ⇒
`instant:null`, status `former`, **excluded from current-value, rendered as a word**.

**Supersession NO LONGER auto-abuts (settled S2-6/§13).** When a new value supersedes and
the source did **not** state the prior's end, the prior is marked **former without
inventing a date** (`valid_to.bound=unknown`, **`certainty="inferred"`**, never `asserted`,
never `now`). Auto-closing a prior interval to a *source-stated* end is the only case that
writes `bound=closed, certainty=asserted`. `overlaps`/`during` relations route to review.

**Recurrence (RFC-5545, lazy):**
```jsonc
"recurrence": { "rrule": "FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=2026-12-31", "dtstart": "2026-01-06",
                "rdates": ["2026-07-04"], "exdates": ["2026-09-08"],
                "overrides": [ { "recurrence_id": "2026-03-17", "patch": {...} } ],
                "tz": "America/Los_Angeles", "count_cap": 730,
                "next_occurrence_at": "2026-06-18T..." }   // CACHED (perf S2-4); expand from max(dtstart,now)
```
Realized set = `(expand(rrule,dtstart,window) ∪ rdates) − exdates`, overrides applied;
never materialized as N rows. **Editing `rrule`/`dtstart` MUST reconcile every existing
`exdate`/`rdate`/`override` `recurrence_id` against the new rule** — re-anchor, drop-with-
audit, or route to review; a dangling exception is **never silently retained** (settled
S2-3/§13). `next_occurrence_at` recomputed on write; "next after now" expands from
`max(dtstart, now)` via `after(dt)`, never from origin; an unbounded high-frequency rule
without a cap is **rejected at the committer** (perf S2-4).

### 2.7 CONCRETE consolidated examples

**(a) Typed scalar — "my A1c was 5.4":** (now passes B2b range gate; "95" from the
glucose clause would FAIL the A1c range + co-location → review.)
```jsonc
{ "schema":"factclaim/2","stage":"resolved","claim_id":"fc_a1c",
  "subject":{"mention":{"surface":"my","span":{"start":0,"end":2}},"entity_id":"ent_self"},
  "predicate":{"raw":"A1c","canonical":"health.a1c","value_shape":"quantity"},
  "value":{"type":"quantity","value":5.4,"unit":"%"},
  "slot":{"cardinality":"set","merge":"add","slot_key":"health.a1c"},
  "kind":"measurement","modality":"asserted",
  "temporal":{"valid_from":{"instant":"2026-06","precision":"month","bound":"closed"},
              "valid_to":{"instant":null,"precision":"instant","bound":"closed"},"status":"ended"},
  "provenance":{"note_id":"n1","span":{"start":0,"end":18},"kind":"extracted"} }
```

**(d) Negation — distinct slot, never overwrites assertion (S1-1):**
```jsonc
// "Sam is NOT allergic to penicillin" — modality=negated is in BOTH keys; it can neither
// supersede nor be superseded by an asserted "allergic to penicillin": they are distinct
// slots that, if both live, raise a CONTRADICTION REVIEW. modality=negated ⇒ excluded from current().
{ "claim_id":"fc_neg","subject":{"entity_id":"ent_sam"},
  "predicate":{"canonical":"health.allergy","value_shape":"text"},
  "value":{"type":"text","value":"penicillin"},
  "slot":{"cardinality":"set","merge":"assert"},
  "kind":"state","modality":"negated",
  "provenance":{"note_id":"n4","span":{"start":0,"end":34},"kind":"extracted"} }
// "if I switch to Acme next year" — modality=hypothetical ⇒ candidate floor, NEVER live;
// 2027 arriving does NOT auto-promote — promotion is an explicit op (S1-2/S2-8).
```

**(f) Former-without-date — "used to work at Acme":** `valid_to.bound=unknown`,
`certainty=asserted` (the source itself says "used to" — a stated former, no invented
date); status=former; excluded from current-value; renders "former (since 2019)".

*(Examples (b) relationship, (c) multi-valued split, (e) recurring carry over from v0
unchanged except: domain absent on the wire; kind a hint; modality gating noted.)*

---

## 3. Storage & graph model

### 3.1 Layers + `fact_assertion` + the materialized `fact_current`

- **Entity node** — stable surrogate; identity resolved, not intrinsic. Split/merge O(1)
  via `redirect_to` on the **canonical-id layer** (§3.5).
- **Fact assertion** — immutable append-only edge row; the audit grain + unit of undo.
- **`fact_current`** — **MATERIALIZED, authoritative-cache** of live-selected current
  values, maintained by the committer in the **same op transaction** (promoted from v0's
  "optional"; perf S1-1). Rebuildable from assertions for audit, so #7 is untouched.

```sql
CREATE TABLE fact_assertion (
  assertion_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id       uuid NOT NULL,
  identity_key   bytea NOT NULL,           -- history/identity key: INCLUDES value+modality (§3.2)
  live_key       bytea NOT NULL,           -- live-selection key: EXCLUDES value for functional, INCLUDES modality
  value_identity bytea,                     -- set-member sub-identity; NULL for functional
  supersedes     uuid REFERENCES fact_assertion(assertion_id),
  op_id          uuid NOT NULL,             -- the fact_op that created this row (undo reads this)
  subject_id     uuid NOT NULL,             -- → same-domain entity projection (§3.5)
  predicate      text NOT NULL, qualifier text,
  value_json     jsonb, object_id uuid,     -- object set iff value_shape=ref
  predicate_kind text NOT NULL,
  cardinality    text NOT NULL,             -- SNAPSHOT of registry at write
  cardinality_source text NOT NULL DEFAULT 'registry',  -- 'registry'|'migration' (M7)
  modality       text NOT NULL DEFAULT 'asserted',
  domain_code    text NOT NULL,             -- COMMITTER-DERIVED; participates in both keys
  lineage_op_kind text,                     -- e.g. 'move_domain' → blocks resurrection ops (security S2)
  confidence     real, pinned boolean NOT NULL DEFAULT false,
  human_touched  boolean NOT NULL DEFAULT false,         -- any human op since last note edit (M3)
  attribution    text,                                   -- 'human_assertion' etc; NON-DROPPABLE, indexed (S4)
  valid_from_instant timestamptz, valid_from_precision text, valid_from_bound text, valid_from_certainty text,
  valid_to_instant   timestamptz, valid_to_precision   text, valid_to_bound   text, valid_to_certainty text,
  valid_from_sortkey timestamptz,           -- precomputed precision-normalized sort key (perf S1-1)
  recurrence     jsonb, next_occurrence_at timestamptz,  -- cached (perf S2-4)
  tx_from        timestamptz NOT NULL DEFAULT now(), tx_to timestamptz,
  reported_at    timestamptz, state text NOT NULL DEFAULT 'live'  -- live|superseded|retracted|tombstone
);
-- live-uniqueness on the LIVE key, restricted to asserted modality:
CREATE UNIQUE INDEX one_live_per_live_key ON fact_assertion (live_key)
  WHERE tx_to IS NULL AND state='live' AND modality='asserted';
-- entity-centric + slot + as-of indexes (perf S1-1, S2-2):
CREATE INDEX fa_subject_live ON fact_assertion (owner_id, subject_id, domain_code)
  WHERE tx_to IS NULL AND state='live';
CREATE INDEX fa_asof ON fact_assertion (owner_id, live_key, tx_from DESC);
CREATE INDEX fa_next_occ ON fact_assertion (owner_id, next_occurrence_at)
  WHERE recurrence IS NOT NULL AND tx_to IS NULL;
```

### 3.2 The TWO keys (override-vs-array + functional-over-time + modality), made mechanical

- **`identity_key`** = `hash(owner, subject, predicate, qualifier, domain, modality, value_component)`
  — the **history/identity** key. Always includes value + modality. "All Acme stints" and
  the supersession chain group cleanly here (fixes S1-4: a query for every Acme interval is
  one clean group, even for functional-over-time).
- **`live_key`** = `hash(owner, subject, predicate, qualifier, domain, modality
  [, value_identity IF cardinality='set'])` — the **live-selection** key. **Functional ⇒
  value excluded ⇒ exactly-one-current.** **Set ⇒ `value_identity` included ⇒ peers.**
- **`modality` in BOTH keys** (S1-1/S1-2): an asserted and a negated claim about the same
  (subject,predicate,value) are **distinct slots**; newest-wins can never flip negation↔
  assertion; if both are live a **contradiction review** fires.
- **`value_identity`** priority: object canonical-id for `ref`; natural key (E.164 phone,
  lowercased email); else a **minted, value-decoupled member-id** carried forward by every
  supersession. **`replace_head` mints/reuses a stable minted `value_identity` decoupled
  from the value's natural key** (settled S2-4): a phone-number correction supersedes the
  member under its stable id; a future re-extraction of the new number matches via the
  member's recorded natural-key map, not by forking a duplicate.
- **`merge_entities` triggers a slot re-key / member-dedup pass** (S1-3 fix): any two live
  set members whose `value_identity` object-ids now resolve to one canonical entity
  collapse to one (audited supersession); else the merge is incomplete. Re-keying a stale
  `bytea` on merge is a defined migration, not a silently-stale hash (S3-1).

### 3.3 Live-selection — THREE gates (modality is new)

`current()` selects on: (1) `tx_to IS NULL` (still believed) AND (2) the valid-time window
(respecting the trichotomy: `unknown` end ⇒ excluded) AND (3) **`modality='asserted'`**.
Non-asserted rows live in the **candidate floor** and are returned only by explicit
candidate queries. **`scheduled`/`expected` future-dated facts do NOT auto-flip to current
when `now` crosses `valid_from`** — realization is an explicit promotion op (S1-2/S2-8).
`reported` (hearsay) is candidate, not current (S3-5). `is_current` is derived; the
materialized `fact_current` (§3.1) is the read-path baseline, rebuildable from assertions.

**Functional-over-time** (e.g. employer history) is **set-storage one-edge-per-value for
clean interval history + a derived "current = latest live asserted by `valid_from_sortkey`"
view** for the exactly-one-current answer (settled §7i, correctness position). The
value-INCLUDING `identity_key` groups history; the value-EXCLUDING `live_key` enforces
one-current; no single key does both jobs.

### 3.4 `fact_op` log (append-only history; frozen outputs; NO precomputed inverse)

```sql
CREATE TABLE fact_op (
  op_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL,
  domain_id int NOT NULL, op_kind text NOT NULL,        -- ~12-op algebra (§4.1)
  actor text NOT NULL,                                  -- 'human:<id>'|'agent'|'reprocess'|'extractor'
  source text NOT NULL,                                 -- capability boundary (allowlist §6)
  target_live_key bytea, payload jsonb NOT NULL,
  resolved_outputs jsonb NOT NULL,                      -- FROZEN: canonical predicate, resolved entity ids,
                                                        --   parsed typed value, cardinality stamp (M8/P2)
  pipeline_tuple jsonb NOT NULL,                        -- extractor/prompt/validator/registry/model versions (M8)
  batch_id uuid, parent_batch_id uuid,                  -- bounded sub-batches share a parent for undo (perf S2-3)
  applied boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE op_checkpoint (                            -- immutable materialized-graph checkpoints (M8/P2)
  checkpoint_id uuid PRIMARY KEY, owner_id uuid NOT NULL,
  as_of_op uuid NOT NULL, graph_snapshot_ref text NOT NULL, created_at timestamptz NOT NULL );
```

One transaction per op (op row + assertion inserts + `fact_current` update commit
together). **No `inverse_of`, no stored inverse** — undo is computed from snapshot +
dependency graph at undo time (§1.2). A pinned live row can't be superseded by
`actor='reprocess'`; **`human_touched` rows are protected beyond the literal pin** (M3).

### 3.5 Entity model (settled §7a — projections + attribute-free global resolution index)

- **Persisted model = F's per-domain `entity_projection`** (one per `(canonical, domain)`,
  holding only that domain's name/attrs) + access-controlled `entity_identity(canonical_id,
  projection_id, domain_id)`. Facts reference a **same-domain projection** — no FK ever
  crosses a firewall (kills the FK covert channel + relink read-oracle).
- **B's `redirect_to`/`canonical_id` mechanics operate on the canonical-id layer** —
  split/merge stay O(1) *within* a domain; cross-domain identity merge is a gated
  `identity_merge` op (O(projections-for-canonical), a redirect rebind, never an assertion
  rewrite).
- **NEW — attribute-free global `canonical` resolution index** (settled by perf S3-1):
  entity resolution runs against a **global, attribute-free embedding/alias index keyed by
  `canonical_id`** (no protectable value, so globally indexable + firewall-safe). Only
  *attribute rendering* goes through per-domain projections. This restores a single,
  globally-indexed resolution target (recall + latency on the ingest hot path) without a
  cross-domain FK or a global attribute row. The cross-domain **resolver** (§6) operates
  over this index, returns only an opaque match/no-match, is constant-work + attribute-
  blind + rate-limited + audited in both domains.

---

## 4. Correction algebra & review submission

### 4.1 The ~12-op closed algebra (shrunk from ~22 — ergonomics F4; undo deletes the inverse-ops)

Typed, named, intent-bearing operations (not JSON-Patch, not corrected-full-record).
Undo is **snapshot-based** (§1.2), so the **inverse-ops are no longer first-class human
ops** (`unretract`/`unpin`/`unmerge`-as-op/`unretime` removed as separate verbs — they are
undo of the forward op). Three spellings of supersede collapse to one cardinality-routed
`replace_member`.

| group | ops | wishlist |
|---|---|---|
| A · per-field | `set_field`{predicate,qualifier,value,kind,**modality**}, `retime`, `set_lifecycle`{retract,pin,confidence} | 1,2,5,6,8,13,14 |
| (firewall)    | **`move_domain`** — own op-type, owner-only, one-way (§6, §7f/g)        | 7 |
| B · entity-link | `relink_subject`, `relink_object`, `mint_and_link_object`, `unlink_object` | 3,4 |
| C · cardinality | `add_to_set`, `remove_from_set`, `replace_member` (subsumes replace_head+supersede) | 9 |
| D · structure | `split_fact`, `merge_facts`, `add_fact` (hardened §4.4) | 10,11 |
| F · identity | `merge_entities`, `split_entity`, `assert_distinct` (entity-targeted; inverses via snapshot-undo) | 12 |

`domain` is a **SCHEMA-ILLEGAL `set_field` field** (settled §7g; security S7, ergonomics
F6) — the closed field enum is `{predicate,qualifier,value,kind,modality}` with `domain`
**absent**, enforced by op schema + unit test. `move_domain` is the only domain-touching
op, and the **LLM/agent can never emit it** (§6). G-temporal ops are the **temporal subset
of `retime`** and enforce: `mark_former` with no date ⇒ `bound=unknown` (never "now");
valid-time edits never rewrite a prior tx version; **rrule/dtstart edits reconcile
exceptions** (§2.6).

**`merge_facts` is constrained (settled S2-2):** (a) **cross-domain merges rejected**
(firewall); (b) **explicit temporal + modality resolution required in op args** (no
heuristic); (c) **rejected when inputs are in a non-trivial Allen relation** that would
fabricate an interval. **`split_fact`/`merge_facts` undo is snapshot+dependency-gated**
(S2-1/M6): undo is legal only with no live dependents; diverged children (relinked/
retimed/retracted) force a cascade preview, never a lossy standalone inverse.

### 4.2 Functional-vs-set rule — `offered_ops` is committer-authoritative

| `functional` | legal value-ops | illegal (UI must not offer; committer rejects) |
|---|---|---|
| `true` | `set_field value`, `replace_member` | `add_to_set`, `remove_from_set` |
| `false` | `add_to_set`, `remove_from_set`, `replace_member` | `set_field value` (hard error) |

`offered_ops` computed by the **committer from the registry, never the client**. Default
for genuinely-ambiguous predicates: **set** (additive is safe; silent-replace is the
dangerous failure) — conditioned on the modality-in-key and member-stability fixes above
(correctness position (j)).

### 4.3 Review submission — fat read / thin write; ONE shared value-shape schema; triage default

Two asymmetric contracts: a **fat read projection** (predicate metadata + `cardinality` +
candidates + enum domains + `ui_capabilities` firewall gates) and a **thin write** =
`{verdict, base_version, ops[]}`. **One value-shape schema (Track A's) imported by both
directions** — no duplicated vocabulary, no codegen-drift surface (settled F8). **One
shared op enum** generated from one schema across C-ops / submission / storage `op_kind`
(F1; kills the triple-naming surface).

**Card IA (settled F1/F5):** the card is a **dumb shell defaulting to Approve / Needs-fix
triage** with progressive disclosure — the 90% path is two keystrokes; only `Needs-fix`
opens the editor. The shell **does NOT branch on `kind` or `reason`** (display hints — a
chip/tint only); structural editors are **`value_shape × cardinality` only** (7×2). `kind`/
`reason` forks and the 6× multiplier are removed. `cardinality` is **not surfaced as a
concept** — "+ add another" renders iff set-valued, omitted otherwise.

**Cell view (settled §7b, conditional-endorse now met):** the payload may render N
temporally-scoped cells; submission + storage **lower every cell to one-assertion-per-
value** via `value_id`↔`value_identity`. With `replace_member`'s stable minted identity
(S2-4) the cell→op lowering is now clean including `replace`+valid-time. **Fat-read for
wide sets is capped/paginated; per-cell candidate enrichment is lazy-loaded** (perf S2-3);
batch transactions are **bounded** (max members-per-batch; large batches split into atomic
sub-batches sharing a parent `batch_id`); per-slot advisory locks acquired in **canonical
slot-key order** (deadlock-safe).

### 4.4 `add_fact` (hardened) + the #7 position

Per spine §1.4. **`add_fact` must cite a real, in-scope provenance note/span AND an
existing subject projection** (so domain is operand-derivable) — **OR** round-trip the
correction-note that re-runs the extractor. The "cite the op as source" escape is
**removed** whenever the subject is freshly minted or domain isn't operand-derivable; in
that case the committer **fails closed** (security S4). `human_assertion` attribution is
**non-droppable + indexed**, distinguishable at every read surface, counted by a
watch-metric; `pin` on a human_assertion/agent fact requires **owner** principal and such
facts are **included** (not exempt) in the unsourced-fact metric + migration diff (security
S8). `add_fact` from an `agent` source may **not** create a cross-canonical link (security
S3/S9).

### 4.5 Audit & undo (snapshot-based; append-only; checkpoints)

One immutable `fact_op` row per applied op carrying frozen `resolved_outputs` +
`pipeline_tuple` + `target_before`/`target_after` snapshots **for display**, plus
`undone_by` (stamped on undo; never deleted). **No executable stored inverse.** Undo
tombstones the op's assertions + un-tombstones what it superseded, gated by the
undo-dependency graph (§1.2). Forensic reconstruction replays *recorded outcomes* to the
nearest **immutable checkpoint** — never genesis, never re-deriving. `add_fact` undo also
retracts/supersedes its auto-drafted correction note (M9). Batch undo operates at batch
granularity through the same dependency graph (M11).

---

## 5. Extraction & reliability (Track D)

### 5.1 Two-stage prompt — with a Stage-1 precision gate

- **Stage 1 — segment + extract** (constrained decode): verbatim surface phrases + cues +
  `source_span`. **NEW: a deterministic fact-bearing-clause precision gate** (has a subject
  + predicate-like relation + a typeable object) filters chit-chat **before** Stage 2,
  eval'd as a precision/recall curve, not one few-shot (model SEV-2.4).
- **Stage 2 — type + link** (constrained decode, per candidate): predicate-registry slice
  + entity candidates injected, turning hallucination surfaces into multiple choice. **NEW:
  retrieval is cached PER NOTE** (one retrieval keyed by the note's mention set, reused
  across candidates) and **batched by shared retrieval context** (perf S2-1); **truncation
  is detected via `finish_reason`** — any non-`stop` finish hard-rejects, never coerces a
  truncated object (model SEV-2.2). Few-shots curated to failure modes incl. **clinical
  negation** ("denies", "ruled out", "r/o", "history-of") (model SEV-1.3).

### 5.2 Deterministic validate → repair → backfill → gate (sole authority)

Two terminal states: validated-commit OR review-item.

- **A · structural:** schema conformance; required-fields keyed off `stage`; contract-
  version pin; **completeness/truncation check** (model SEV-2.2).
- **B · grounding:** **B1** span verification; **B2** typed-value re-derivation over a
  **verified-correct span**; **B2b** registry plausibility-range + predicate/value
  co-location (model SEV-1.1, correctness S2-5); **B3** modality cross-check — **health/
  finance: ANY lexicon hit OR model-low-confidence on modality ⇒ mandatory review**, and a
  Stage-2 modality re-ask requires the polarity-establishing words be **quoted in the span**
  (model SEV-1.3); **B4** provenance integrity.
- **C · vocabulary:** enum coercion; predicate canonicalization (embedding-assisted) with
  **a review band just below the merge threshold** (a coined predicate landing near an
  existing one routes to review, never auto-merges — model SEV-2.3; threshold owned by
  registry config with a wrong-merge eval gate); **C3 cardinality stamping from the
  registry** + the additive-cue conflict→review guard (§2.4); cache canonicalization by
  `raw_predicate` (perf S2-5).
- **D · link/firewall (100% tested):** entity existence in RLS scope; firewall guard;
  candidate-rank consistency (extended to **canonical cross-domain footprint** — security
  S5); self-link guard.
- **E · repair:** structured re-ask, **capped N=2**, **error messages are typed closed-enum
  codes** ("ERR_SPAN_MISMATCH") that **never echo note/model content** (closes the
  injection/evade-oracle channel — model SEV-2.2); re-asks bounded **per note** (a note
  generating many re-asks → whole-note review); then graceful degradation to review.
- **F · calibration:** confidence clamp + recalibration (per-domain curve versioned in the
  pipeline-tuple, no cross-firewall leak); backstop-firing budget.

**Closed inference templates (settled §7d):** `kind=inferred` is admissible **only** via a
**closed set of typed inference templates** (`age_to_birthyear{anchor_date,stated_age}`,
`ordinal_to_count`, `relative_date{anchor,offset}`, unit-literal conversions), **each with
a deterministic verifier** that recomputes the value from cited *literal* spans. Anything
outside the set is **not** an inferred fact — it is `add_fact`/review. Inferred facts are
**capped as a fraction of a note** (mostly-inference note → whole-note review). An eval
**injection slice specifically abuses the `inferred` flag** with a zero-tolerance gate.

### 5.3 Contract versioning + re-analysis = 3-WAY diff over the human-op overlay

SemVer: patch (additive — no re-extract); minor (new required field, deterministic backfill
— no model calls); major (shape change — planned re-analysis). Re-analysis is a scheduled
Phase-5 workflow with a hard token/$ budget gate. **The diff is 3-WAY** (settled M2/M3):
`(old machine facts ⊕ human-op overlay)` vs `new machine facts`, **not** 2-way:

- **Retractions/removals suppress re-extraction** (M2): a re-emitted claim whose
  `identity_key` (+ `value_identity`) matches a **human retraction/removal post-dating the
  most recent supporting note edit** routes to review ("you retracted this; re-extraction
  re-proposed it"), never auto-accepts.
- **Human-touched ≠ only pinned** (M3): any field carrying a `human:*` op since the last
  note edit is **frozen against re-extraction**; conflicting re-extracted values route to
  review. ("Pinned" = explicitly protected; "human-edited" = implicitly protected.)
- **Split lineage** (S2-1): a re-extracted claim whose provenance span overlaps a span
  already covered by a human `split_fact`/`merge_facts`/`add_fact` routes to review.
- **Major migration per-pin path** (M3): a pinned v(n) fact is migrated by deterministic
  shape-lift where one exists, else **explicitly enumerated as a migration blocker** with a
  bounded review queue + visible count (the migration SLA); never silently re-shaped, never
  left to break the version pin. Cutover completes when blockers = 0.
- **Incremental scope** (perf S2-1): re-extract only notes whose facts touch the changed
  contract field (blast radius computed from the diff *before* running); reserve whole-
  corpus re-extraction for genuine shape changes.
- **No-op suppression** (perf S2-2/S2-5): do not write an assertion on reprocess when the
  re-derived fact is byte-identical to the live row (content-hash short-circuit); skip the
  full backstop pass when note span + contract version are unchanged.

### 5.4 Eval gates

Frozen golden set (multi-valued, typed quantities/dates with precision, every modality,
existing-vs-mint links, recurrence, over-extraction negatives). Per-field semantic metrics
via bipartite alignment. **Zero-tolerance gates: negated/hypothetical→asserted;
hallucinated-link; committed-value-error on health/finance measurement scored against gold
even when UNALIGNED** (model SEV-1.1 — a wrong-span wrong value must not hide inside a
tolerance-banded recall dip); **structural-coherence rate** (grammar-valid-but-invariant-
violating emissions, model SEV-1.4); **inferred-flag-abuse injection slice** (model
SEV-1.2); **clinical-negation slice** (model SEV-1.3); **wrong-merge rate on
distinct-but-similar predicate pairs** (model SEV-2.3); **oversized-note no-truncated-
commit slice** (model SEV-2.2); **over-extraction precision + mint-duplicate watch-metrics**
(model SEV-2.4). CI hermetic via cassettes (LLM faked); backstop-ablation proves the net;
adversarial/jailbreak slice ties to F.

### 5.5 Existing-corpus migration — SUPERSEDED BY DECISION D1 (clean rebuild)

**D1 (`01-decisions-log.md`): the initial cutover is a CLEAN REBUILD** — drop the derived
graph and re-ingest all retained notes under the new contract. There is **no in-place legacy
migration**, so the one-time mapping below is **OUT OF SCOPE** for the cutover (Round-1 M7
reclassified from must-fix to out-of-scope). The new contract produces cardinality,
`value_identity`, and the bound trichotomy natively on re-ingest; the `valid_to=NULL`
ambiguity and silently-replaced-history loss simply don't arise (nothing to import).

The mapping is retained below **only as reference for FUTURE re-analysis** under a later
contract version (where notes are re-extracted and the human-op overlay + pinned/human-touched
protection of §5.3 apply) — not for the initial cutover:
- **Cardinality:** stamp from today's registry; record `cardinality_source='migration'` so
  a later correction is a tracked re-key op, not a silent flip.
- **Member identity:** mint `value_identity` per legacy member; flag the migrated set
  `kind='migrated'` + low confidence so a later note **re-anchors** members rather than
  forking them. Pre-migration silently-replaced history is **unrecoverable — stated
  explicitly** (ACCEPTED-RISK).
- **Bound trichotomy:** legacy `valid_to=NULL` → `bound='unknown'` only when status was
  "former"-flagged; else `open`; **where legacy data can't tell, migrate to `unknown` +
  route to review** (conservative — never fabricate "ongoing"). Document the heuristic +
  its error rate.
- **Transaction time:** backfill `tx_from`/`reported_at` from legacy capture timestamps,
  **not `now()`** (M10), so historical as-of queries work pre-migration.

---

## 6. Security (Track F)

**Four rules bound the expanded surface to near-zero new firewall risk:**

1. **The privileged committer is the sole writer.** Revoke direct DML from the app role;
   only the committer applies ops. The committer **re-derives `domain_id` from operands**
   (subject projection's domain + provenance note's domain) and **ignores any claimed
   domain** — and **fails closed when it cannot independently re-derive domain from a
   non-op-controlled operand** (security S4 — never falls back to a claimed value; this is
   why `add_fact` must cite a real note + existing subject). `SET LOCAL` GUC per txn; unset
   scope ⇒ zero rows + write fails closed; `FORCE ROW LEVEL SECURITY`; no `BYPASSRLS`.

2. **Per-domain projections + attribute-free global resolution index** (§3.5). Facts
   reference a same-domain projection (no cross-domain FK, ever). Firewall enforced at
   **value materialization** (a session may reference an out-of-scope canonical but never
   render its protected attributes). **No general fact may associate its object to a
   `canonical_id` that has a protected (health/finance/location) projection** except via an
   owner-gated `identity_merge` — the §2.4-rule-3 "associate to existing canonical" side-
   door is closed for protected canonicals (security S3 — kills the emergent movement
   oracle). The committer's "does this canonical have a protected projection?" check is a
   **gated, audited resolver query**, never an integrator convenience.

3. **Cross-domain resolver security contract (NEW — the v0 unbuilt asset; security S1).**
   The resolver is a **separately-privileged, audited, rate-limited** service that (a)
   returns only an opaque `canonical_id` match/no-match — **never a cross-domain
   attribute**; (b) is **constant-work / constant-time** w.r.t. the protected side (always
   runs the full candidate set, decoy-padded, no early-exit on a protected match — kills the
   timing oracle); (c) emits **no** candidate-rank/confidence/"matched-on" signal into any
   general-scoped row (`candidate_ids` general-only); (d) invocations are rate-limited per
   session + **audited in both domains**; (e) the *decision* to merge identities is an
   owner-gated `identity_merge` op, **never** an automatic integrator side-effect from
   untrusted-note volume. Operates over the attribute-free global index (§3.5), not over
   both domains' full projection rows.

4. **Domain DOWNGRADE is one-way, owner-only, copy-forward** (settled §7f/§7g; security
   S2/S6, migration M4): **(a)** the LLM/agent can **never** emit `move_domain`; **(b)**
   owner-only (`source='human:owner'`); **(c)** explicit, non-batchable, non-pre-filled
   confirmation showing exactly which values + provenance become visible; **(d) copy-
   forward, ONE-WAY** — a new general fact + projection cites the original; the original is
   marked `superseded`; **undo PURGES the general row's `value_json` (tombstone), not
   retract** — nothing intra-general can resurrect it; re-protecting requires authoring a
   **new** fact, not unwinding; `retime`/`unretract`/`supersede` are **forbidden on any row
   whose `lineage_op_kind='move_domain'`** (kills the move→undo→retime launder); **(e)**
   bounded, rate-limited, no wildcard, cascade explicit. The general-domain audit row for a
   move is **redacted to the moved value only** — no `batch_id`, source-domain, or sibling-
   count (kills the audit-timing oracle, security S6); cross-domain audit linkage lives only
   in an owner-scoped all-domains view.

**Op-type allowlist by source:** `extractor` and `agent` may emit
`{set_field, add_to_set, remove_from_set, replace_member, relink(in-scope, non-protected-
canonical), retime, …}` but **never** `{move_domain, cross-domain identity_merge, pin,
retract-of-pinned, cross-canonical add_fact/relink}` (security S9 — agents consume
untrusted graph content, same hard allowlist as extractor). **`extractor`-sourced `relink`
whose new object's canonical has any cross-domain footprint is a PROPOSAL → review**, never
auto-committed (security S5). A human edit and a model proposal traverse the identical
committer + RLS path; the editable surface is wide, the commit surface is the narrow
chokepoint.

**Isolation tests (per new table; real Postgres testcontainers; `S_general`/`S_health`):**
v0's seventeen PLUS the nine R1 security tests — S-test 1 (confused-deputy `add_fact` fail-
closed), S-test 2 (`set_field{domain}` schema-rejected), S-test 3 (move audit metadata
redaction), S-test 4 (move→undo→retime corpus, value purged), S-test 5 (resolver attribute-
and timing-blind), S-test 6 (extractor relink to cross-canonical → review), S-test 7
(general edge to protected canonical rejected; movement-oracle fixture unconstructable),
S-test 8 (non-droppable attribution at every read surface + owner-only pin), S-test 9
(agent allowlist parity with extractor). Every new table ships its RLS isolation test in
the same PR (CLAUDE.md rule 3).

---

## 7. CONFLICT DECISIONS — now mostly settled

**(a) Entity model — SETTLED:** per-domain projections + **attribute-free global resolution
index** + B's redirect on the canonical-id layer (§3.5). Resolves the security worst-shape
AND the perf recall/latency break AND keeps O(1) reversible split/merge. *Was provisional
hybrid; now settled with the global resolution index added (perf S3-1) and the resolver
security contract specified (security S1).*

**(b) One-claim-per-value vs cells — SETTLED:** cells in the read projection, one-edge-per-
value in submission + storage. The conditional endorse is now **met**: `replace_member`'s
stable minted `value_identity` (S2-4) makes the cell→op lowering clean including
replace+valid-time; fat-read is capped/paginated (perf S2-3). *Was provisional + unproven;
now proven.*

**(c) Value-typing authority — SETTLED:** parser authoritative **BUT guarded** by registry
plausibility-range (B2b) + predicate/value co-location + within-variant low-confidence
review (§2.2). Parser wins on *how to type*; it is never authority on *which span/number is
the predicate's value*. *Was provisional (2); now strengthened — the "agree on a wrong
span" hole is closed.*

**(d) Inferred facts — SETTLED:** a **closed set of deterministically-verified inference
templates** only; model-authored free-form derivation traces **rejected**; anything else is
`add_fact`/review; capped per note; injection-abuse eval slice (§5.2/§5.4). *Was provisional
(3) "inferred flag exempt + review"; now hardened — the forgeable-trace hole is closed.*

**(e) `add_fact` — SETTLED toward (1)+(2) conditional:** direct `add_fact` only with a real
in-scope note + existing subject (domain operand-derivable); otherwise **forced correction-
note round-trip**; non-droppable indexed attribution; watch-metric (§4.4). *Was provisional
(1) flag-only; security S4 forced the note/existing-subject condition.*

**(f) Domain-move reversibility — SETTLED toward (2)/(iii) one-way:** copy-forward
mechanics kept, but downgrade is **irreversible** — undo purges the general value
(tombstone); re-protection authors a new fact; move-lineage rows are non-resurrectable
(§6.4). *Was provisional (3) reversible+cite-tracking; security S2 + migration M4 both
realized the laundering flip-condition.*

**(g) `set_field` super-op — SETTLED toward (2):** `domain` hoisted out into `move_domain`
AND made a **schema-illegal `set_field` field** (closed enum excludes it, enforced by test).
*Was provisional (2); strengthened from "not offered" to "unrepresentable" (security S7,
ergonomics F6).*

**(h) Model-emitted `slot.merge` — SETTLED:** ban kept (model emits `assert` only), **plus**
the additive-cue-vs-functional-registry conflict→review guard so a wrong registry flag can't
silently destroy a member (model SEV-2.1/§2.4).

**(i) Functional-now vs functional-over-time — SETTLED with TWO identities:** value-
INCLUDING `identity_key` for history grouping + supersession chain; value-EXCLUDING
`live_key` for exactly-one-current; functional-over-time stored as one-edge-per-value with a
derived "latest live asserted" current view (§3.2/§3.3). *Was the most-cited open item
(S1-4); now resolved.*

**(j) Ambiguous-cardinality default — SETTLED `set`**, conditioned on the now-shipped
modality-in-key (S1-1/S1-2) and member-stability (S1-3/S2-4) fixes.

**(k) `structured` variant — SETTLED closed**, registry-declared, with a `propose_shape`
review fast-path; never model-coined (model SEV-2.3).

**(l) Corroboration — SETTLED add a provenance row**, guarded: same-domain only, re-ground
the corroborating span (B1/B2), append-only audited child table (model (l)).

**STILL GENUINELY OPEN (surfaced to the user):** none are Sev-1/Sev-2 *blockers*, but two
design choices remain judgement calls — (i) whether `boolean`/`structured` ship now or
build-on-demand (ergonomics F7, a nit); (ii) the exact wrong-merge similarity threshold +
review band width for predicate canonicalization (needs the labeled pair set to tune — a
tuning task, not an open design question).

---

## 8. Invariant check against framing §4

- **LLM-adapter only** — ✔ model proposes via the adapter; committer sole writer.
- **Storage abstraction** — ✔ committer + `fact_current` + checkpoints via the abstraction.
- **RLS domain firewalls** — ✔ strengthened: projections + attribute-free global resolution
  index + **resolver security contract** + committer domain re-derivation **failing closed**
  + **one-way** downgrade + non-resurrectable move-lineage + audit redaction + agent
  allowlist parity. Every new table ships an RLS isolation test (v0's 17 + R1's 9). *Residual
  (ACCEPTED-RISK): the global resolution index is a high-value asset — mitigated to
  attribute-free + constant-work + audited, not eliminated.*
- **Bitemporal model** — ✔ valid-time (trichotomy, no auto-abutment) + tx-time independent;
  as-of indexed (perf S2-2).
- **Audit & reversibility** — ✔ append-only assertions + op attribution + **snapshot-based
  undo with an explicit dependency graph** (cascade or block — no false "total inverse"
  claim) + immutable checkpoints. Genesis-replay dropped; ops freeze resolved outputs +
  pipeline-tuple. Unmerge-after-writes is a reviewed split (M5). *Residual: undo of deeply-
  composed cross-family op chains still requires the dependency-graph walk — bounded and
  defined, not magic.*
- **Machine-written wiki (#7)** — ✔ no doctrine change; `add_fact` hardened (real note +
  non-droppable attribution); materializations are rebuildable caches.
- **Conventional Commits / branch+PR / CI-green / tests-with-code** — process constraints on
  the build; no code written here.

**Wishlist coverage:** all 15 §2 items map to the ~12-op algebra (§4.1); override-vs-add is
explicit via the two keys + `offered_ops`; the review surface is one triage shell + 7×2
value-editors keyed on `(value_shape, cardinality)`; `kind`/`reason` are display hints.
Success criteria re-baselined: **"fewer kinds" is now measured as DECISION POINTS a
maintainer touches** (one shared op enum, one value-shape schema, snapshot-undo deleting ~22
inverse defs), not component count (F1).

---

## 9. ROUND-1 DISPOSITION TABLE

Every Sev-1/Sev-2 finding → Resolution.

### Correctness
- **S1-1** (negation re-asserts via shared key) — **FIXED:** `modality` in BOTH keys + `current()` filters asserted-only; asserted-vs-negated on same value = contradiction review, never supersession (§1.5, §3.2/§3.3).
- **S1-2** (`current()` ignores modality) — **FIXED:** modality is the 3rd live-selection gate; non-asserted → candidate floor; explicit promotion op (§3.3).
- **S1-3** (set-member identity drift / resurrection / fork) — **FIXED:** tombstone-vs-readd → review; `merge_entities` re-key/dedup pass; value-decoupled minted member-id (§3.2).
- **S1-4** (functional-over-time unresolved, two readings differ) — **FIXED:** two identities — value-including history key + value-excluding live key (§3.2/§3.3, §7i).
- **S2-1** (split→re-extract not idempotent) — **FIXED:** 3-way diff + split-lineage span-overlap → review (§5.3).
- **S2-2** (`merge_facts` temporal/modality/domain undefined) — **FIXED:** cross-domain rejected; explicit temporal+modality args; non-trivial Allen → reject (§4.1).
- **S2-3** (recurrence exception breaks on rule edit) — **FIXED:** rrule/dtstart edits reconcile every exdate/rdate/override or route to review (§2.6).
- **S2-4** (`replace_head` + valid-time → dup or two live) — **FIXED:** `replace_member` mints/reuses a value-decoupled stable `value_identity` (§3.2).
- **S2-5** (locale/within-variant misparse not caught) — **FIXED:** B2b range + within-variant low-confidence review + registry locale/unit context (§2.2/§5.2).
- **S2-6** (Allen auto-abutment fabricates end date) — **FIXED:** auto-abutment dropped; supersession marks former without a date (`bound=unknown`/`certainty=inferred`), never `asserted`/`now` (§2.6).
- **S2-7** (bitemporal undo leaves two live / gap) — **FIXED:** snapshot-undo re-validated against current state + dependency graph blocks/cascades non-composable undo (§1.2, §4.5).
- **S2-8** (`scheduled`/`expected` auto-flip to current) — **FIXED:** no auto-promotion on `now` crossing `valid_from`; explicit realization op (§3.3).

### Model-compliance
- **SEV-1.1** (parser+model agree on wrong span) — **FIXED:** B2b registry range + predicate/value co-location + committed-value-error zero-tolerance eval even when unaligned (§2.2/§5.2/§5.4).
- **SEV-1.2** (inferred derivation-trace forgeable) — **FIXED:** closed inference-template verifiers only; free-form rejected; per-note cap; injection-abuse eval slice (§5.2/§7d).
- **SEV-1.3** (modality trusted; lexicon not an oracle) — **FIXED:** health/finance any-hit-or-low-conf → mandatory review; modality re-ask quotes span words; clinical-negation eval slice (§5.2).
- **SEV-1.4** (constrained decode ≠ cross-field invariants) — **FIXED:** R5 bi-conditional both-directions tested; committer re-derives `kind` from value.type+value_shape; structural-coherence eval metric (§2.1/§5.4).
- **SEV-2.1** (registry mis-flag silently supersedes) — **FIXED:** additive-cue-vs-functional conflict → review; low-confidence flags default to set (§2.4).
- **SEV-2.2** (N=2 re-ask injection + truncation commit) — **FIXED:** typed closed-enum error codes (no content echo); `finish_reason` truncation hard-reject; per-note re-ask bound; oversized-note eval slice (§5.1/§5.2/§5.4).
- **SEV-2.3** (coined-predicate/shape wrong-merge or sprawl) — **FIXED:** review band below merge threshold; closed `structured` set; registry-owned threshold + wrong-merge eval gate (§2.2/§5.2/§5.4).
- **SEV-2.4** (over-extraction flood + mint duplicates) — **FIXED:** Stage-1 deterministic precision gate; provisional flagged mint + deferred dedup; precision/mint-dup watch-metrics (§5.1/§2.3).

### Security
- **S1** (cross-domain resolver read-oracle) — **FIXED:** resolver security contract — attribute-blind, constant-work, rate-limited, dual-domain audited, owner-gated merge decision (§6.3).
- **S2** (move→undo→retime laundering) — **FIXED:** one-way downgrade; undo purges value (tombstone); move-lineage rows non-resurrectable (§6.4, §7f).
- **S3** (location object-link + add_fact movement oracle) — **FIXED:** no general→protected-canonical association except owner `identity_merge`; agent barred from cross-canonical add_fact (§6.2, §4.4).
- **S4** (confused-deputy committer on add_fact) — **FIXED:** committer fails closed when domain not operand-derivable; add_fact must cite real note + existing subject; non-droppable attribution (§6.1, §4.4).
- **S5** (extractor in-scope relink steered by injection) — **FIXED:** extractor relink to cross-footprint canonical → review, not auto-commit (§6 allowlist).
- **S6** (audit read = batch/timing oracle) — **FIXED:** move audit redacted to moved value only; no batch_id/source-domain/sibling-count in general-scoped audit (§6.4).
- **S7** (`set_field{domain}` latent) — **FIXED:** `domain` schema-illegal `set_field` field, enforced by schema + test (§4.1, §7g).
- **S8** (pin laundering past migration) — **FIXED:** pin on human_assertion/agent requires owner; such facts included in unsourced metric + migration diff (§4.4).

### Migration
- **M1** (precomputed inverse undoes wrong state) — **FIXED:** snapshot-based undo + undo-dependency graph; cascade or block; "remove k keep k+1" is a forward correction (§1.2, §4.5).
- **M2** (re-analysis resurrects retracted facts) — **FIXED:** 3-way diff; retraction/removal post-dating note edit → review (§5.3).
- **M3** (pinned/human-edited overwritten by migration) — **FIXED:** protect human_touched not just pinned; per-pin shape-lift-or-blocker path with SLA (§3.4/§5.3).
- **M4** (downgrade laundering via downstream derivation) — **FIXED:** one-way downgrade (same as S2); re-protection is a new fact (§6.4, §7f).
- **M5** (entity unmerge unsafe after downstream writes) — **FIXED:** unmerge-after-writes is a reviewed split with per-fact assignment; cheap link-clear only with proven no-write window (§4.1, §8).
- **M6** (split↔merge inverses not clean after divergence) — **FIXED:** snapshot+dependency-gated undo; diverged children force cascade preview (§4.1).
- **M7** (corpus migration loses cardinality/member-id/bound) — **OUT OF SCOPE per decision D1 (clean rebuild):** the initial cutover drops the derived graph and re-ingests notes under the new contract, so there is nothing to migrate in place and the lossy-mapping risk disappears (§5.5). The §5.5 mapping is retained only for FUTURE contract-version re-analysis. *(Supersedes the prior "FIXED via migration spec" disposition.)*
- **M8** (genesis-replay non-replayable + unbounded) — **FIXED:** genesis-replay dropped; ops freeze resolved outputs + pipeline-tuple; immutable checkpoints bound replay (§1.2, §3.4).

### Ergonomics
- **F1** ("fewer kinds" not met; god-component) — **FIXED:** one shared op enum + one value-shape schema; card is dumb triage shell, `value_shape×cardinality` editors only; `kind`/`reason` are hints; metric re-baselined to decision points (§4.3, §8).
- **F2** (op-log/inverse over-built) — **FIXED:** snapshot-based undo; ~22 inverse defs + migration ladder deleted (§1.2, §4.5).
- **F3** (fat envelope false economy) — **FIXED:** honest typed stage shapes `ExtractedClaim→ResolvedFact→ReviewCard` (§2).
- **F4** (~22-op algebra not minimal) — **FIXED:** shrunk to ~12; inverse-ops removed; three supersede-spellings → `replace_member` (§4.1).
- **F5** (card over-edits; offered_ops footgun) — **FIXED:** default Approve/Needs-fix triage + progressive disclosure; cardinality concept hidden (§4.3).
- **F6** (split `domain` out of `set_field`) — **FIXED:** done + schema-illegal (§4.1, §7g).

### Performance
- **S1-1** (current-value derived at read, unbounded plan) — **FIXED:** `fact_current` materialized authoritative-cache + entity/slot/as-of indexes + precomputed `valid_from_sortkey` (§3.1, §3.3).
- **S2-1** (Stage-2 per-candidate cost; whole-corpus re-extract) — **FIXED:** per-note cached retrieval + shared-context batching + incremental migration scope from the diff (§5.1/§5.3).
- **S3-1** (projections multiply + un-indexable resolver on hot path) — **FIXED:** attribute-free global resolution index restores a single indexed resolution target; identity_merge is a redirect, not an assertion rewrite (§3.5).
- **S2-2** (append-only growth; as-of scans) — **FIXED:** `(owner,live_key,tx_from DESC)` index/BRIN + no-op reprocess suppression + live/archived partitioning option (§3.1, §5.3).
- **S2-3** (O(members) op rows + lock contention) — **FIXED:** capped/paginated fat-read, lazy per-cell enrichment, bounded sub-batches, canonical-order advisory locks (§4.3).
- **S2-4** (rrule next-occurrence unindexed/unbounded) — **FIXED:** `next_occurrence_at` cache + index; expand from `max(dtstart,now)`; uncapped high-freq rule rejected at committer (§2.6).
- **S2-5** (per-fact backstop CPU + vector query on reprocess) — **FIXED:** cache canonicalization by raw_predicate; reuse Stage-2 retrieval for D1; content-hash short-circuit on reprocess (§5.2/§5.3).

---

*End spec v1. Settled: §7 (a)–(l). Residual ACCEPTED-RISK: the attribute-free global
resolution index remains a high-value asset (mitigated, not eliminated); pre-migration
silently-replaced set history is unrecoverable (stated). No Sev-1/Sev-2 left unresolved;
the two genuinely-open items (boolean/structured timing; canonicalization threshold tuning)
are nits/tuning tasks, not blockers.*
