# Track B · Storage & graph model — how facts persist as a property graph in Postgres

**Status:** Phase-1 research brief (greenfield, first-principles). Owner: Track B.
**Reconciles against:** `00-framing.md` §3 (communication structure), §4 (invariants).
**Coordinates with:** Track A (in-flight fact shape), Track C (edit-operation algebra
this layer must persist), Track F (deep RLS/firewall security), Track G (temporal &
recurrence semantics). This brief owns *persistence and identity*; it consumes A/C/G's
shapes and exposes the mutation surface they need.

---

## 0. TL;DR

Persist facts as an **append-only, bitemporal, one-edge-per-value property-graph store**.
The atomic unit is the **fact assertion** (an immutable row). The mutable thing a user
"has" — "Sam's employer", "Jeff's children" — is the **fact slot**, a derived/keyed
grouping over assertions. The override-vs-array problem is solved by making the
**identity key** of a slot *predicate-cardinality-aware*: functional predicates key
**without** the value (so a new value supersedes the old), set-valued predicates key
**with** a value-identity component (so a new value is a *peer*, not a replacement).
Every edit is a typed, reversible, audited storage mutation expressed as
**new immutable rows** (supersede / re-open), never an in-place `UPDATE` of fact content.

---

## 1. Proposal

### 1.1 Core model: assertion vs. slot vs. node

Three layers, separated so identity, history, and "current truth" never fight:

- **Entity node** (`entity`): a thing facts are about or point to (a person, org, place,
  account, the note's author). Has a stable surrogate id and a separate human-resolvable
  identity (names/aliases). Split/merge operate here.
- **Fact assertion** (`fact_assertion`): the immutable, append-only edge row —
  `subject —predicate[.qualifier]→ value | object`, plus modality, kind, domain, the
  bitemporal columns, provenance, confidence. **Never updated in place.** This is the
  audit grain and the unit of reversibility.
- **Fact slot** (the *logical* fact): the identity key (§3) that groups assertions which
  are "the same fact over time." For a functional predicate a slot holds one live value
  at a time (a history of supersessions); for a set-valued predicate a slot is the *set*
  and each member has its own sub-identity. The slot is **not a stored table** by
  default — it is the `slot_key` column on the assertion plus a partial index that picks
  the live row(s). (Alternative: materialize it; see §5.4.)

This is the property-graph analogue of SCD-Type-2 with a bitemporal twist: assertions are
versioned rows; the "current row" is the one whose transaction-time is open *and* whose
valid-time interval matches the query instant.

### 1.2 The override-vs-array core, stated precisely

The framing's central bug ("add another silently replaces the head") is, at the storage
layer, an **identity-key collision**. If "employer" and a *new* employer hash to the same
slot key, the new assertion looks like a supersession of the old — replacement. If they
hash to *different* slot keys, they coexist — addition. So:

> **The cardinality of a predicate is a property of its identity key, not a flag checked
> by application code at write time.** Functional ⇒ value is *excluded* from the key.
> Set-valued ⇒ a stable **value-identity** is *included* in the key.

This makes the three set operations mechanical and unambiguous (§3.3):
- **add a value** → a new assertion with a *new* `value_identity` → new live member.
- **replace the head** → supersede the *targeted member's* current assertion (same
  `value_identity`) with a new one.
- **remove one** → tombstone *that member's* `value_identity` (a retraction assertion),
  leaving the rest of the set live.

No write path ever has to "guess" override-vs-add: the edit operation (Track C) names the
target member explicitly, and the storage key does the rest.

### 1.3 Append-only + reversibility

Every committed change is one or more inserts. Reversibility = the inverse is also an
insert (a compensating assertion or re-open), so the audit log is the undo log. We never
delete history; "remove" is a tombstone, "supersede" closes a transaction-time interval,
"undo" re-opens it. This satisfies §4 audit & reversibility and the §4-#7 doctrine
(see §6.1): humans never edit prose or rows; they *emit correction operations* the engine
applies as machine writes.

---

## 2. Entity nodes & identity

```sql
-- 2.1 Entity node: the stable surrogate; identity is resolved, not intrinsic.
CREATE TABLE entity (
  entity_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id       uuid NOT NULL,                 -- RLS principal (single-owner system)
  entity_kind    text NOT NULL,                 -- person|org|place|account|... (registry)
  display_name   text NOT NULL,                 -- best current label; derived, correctable
  domain_code    text NOT NULL DEFAULT 'general', -- firewall band of the node itself
  status         text NOT NULL DEFAULT 'active', -- active|merged_away|split_away
  redirect_to    uuid REFERENCES entity(entity_id), -- set when merged: follow to survivor
  created_at     timestamptz NOT NULL DEFAULT now(),
  created_by     text NOT NULL                  -- 'extractor' | 'integrator' | review-op id
);

-- 2.2 Aliases / identity evidence (names, external ids). Many-per-entity = set-valued
--     by construction, so it is its own table, not a column.
CREATE TABLE entity_alias (
  alias_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id    uuid NOT NULL,
  entity_id   uuid NOT NULL REFERENCES entity(entity_id),
  alias_kind  text NOT NULL,         -- name|nickname|email|handle|external_id
  alias_value text NOT NULL,
  embedding   vector(384),           -- reuse entity-resolution infra
  valid_from  timestamptz, valid_to timestamptz,  -- "known as X until Y"
  asserted_at timestamptz NOT NULL DEFAULT now(),
  retracted_at timestamptz           -- soft, for audit; null = live
);

-- 2.3 Distinctness assertions ("these two are NOT the same Sam"). Blocks future merge.
CREATE TABLE entity_distinct (
  owner_id   uuid NOT NULL,
  a_id       uuid NOT NULL REFERENCES entity(entity_id),
  b_id       uuid NOT NULL REFERENCES entity(entity_id),
  asserted_by text NOT NULL, asserted_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (a_id, b_id)
);
```

**Identity-key for entities** is *not* the surrogate (that is just a handle); it is the
alias/embedding cluster plus distinctness constraints. **Split/merge** never rewrite the
millions of assertion rows that point at an entity — they flip `entity.status` +
`redirect_to` and resolution follows redirects (§3.5). This keeps split/merge O(1) and
trivially reversible (clear `redirect_to`).

---

## 3. The fact assertion (edge/fact record)

```sql
CREATE TABLE fact_assertion (
  -- ── identity & lineage ───────────────────────────────────────────────
  assertion_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id       uuid NOT NULL,                       -- RLS principal
  slot_key       bytea NOT NULL,                      -- the identity key (§3.1), derived
  value_identity bytea,                               -- set-member sub-identity (§3.2); NULL for functional
  supersedes     uuid REFERENCES fact_assertion(assertion_id), -- the row this revises
  op_id          uuid NOT NULL,                       -- the write/edit op that created this row (audit)

  -- ── the triple ───────────────────────────────────────────────────────
  subject_id     uuid NOT NULL REFERENCES entity(entity_id),
  predicate      text NOT NULL,                        -- canonical (Track A/predicate registry)
  qualifier      text,                                 -- e.g. nickname audience; part of slot key
  value_json     jsonb,                                -- typed literal per value_shape (Track A)
  object_id      uuid REFERENCES entity(entity_id),    -- set iff value_shape = ref(<kind>)

  -- ── classification ───────────────────────────────────────────────────
  predicate_kind text NOT NULL,        -- event|measurement|state|attribute|preference|relationship
  cardinality    text NOT NULL,        -- 'functional' | 'set'  (snapshot of registry at write)
  modality       text NOT NULL DEFAULT 'asserted', -- asserted|negated|hypothetical|reported|question|expected
  domain_code    text NOT NULL,        -- firewall band (health|finance|location|general)
  confidence     real,
  pinned         boolean NOT NULL DEFAULT false,

  -- ── BITEMPORAL ───────────────────────────────────────────────────────
  -- valid time: when the fact is true in the world (Track G owns precision/recurrence)
  valid_from     timestamptz,                          -- NULL = unbounded-past / unknown
  valid_to       timestamptz,                          -- NULL = ongoing
  valid_precision text NOT NULL DEFAULT 'unknown',     -- instant|day|month|year|era|unknown
  recurrence     jsonb,                                -- rrule (Track G)
  -- transaction (reported/system) time: when WE recorded it; closed on supersede
  tx_from        timestamptz NOT NULL DEFAULT now(),
  tx_to          timestamptz,                          -- NULL = currently-believed; set = revised/retracted
  reported_at    timestamptz,                          -- when the SOURCE reported it (note's own claim time)

  -- ── lifecycle ────────────────────────────────────────────────────────
  state          text NOT NULL DEFAULT 'live',         -- live | superseded | retracted | tombstone
  CHECK ( (object_id IS NULL) <> (value_json IS NULL)  -- exactly one of value/object, or...
          OR predicate_kind = 'relationship' )         -- (relationship may carry both)
);
```

### 3.1 The slot key (identity) — the heart of the design

`slot_key = hash(owner_id, subject_id, predicate, COALESCE(qualifier,''), domain_code
[, value_identity if cardinality='set'])`

Computed deterministically at write time from the canonicalized predicate + resolved
subject. Properties:

- **Functional predicate** (`birthDate`, `currentEmployer.head`, `heightCm`): key excludes
  the value. All assertions of a person's birth date share one `slot_key`; a new value with
  the same key *supersedes*.
- **Set-valued predicate** (`employer`, `child`, `phoneNumber`): key *includes*
  `value_identity` (§3.2). Two different employers → two slot keys → coexisting live rows.
- **Qualifier participates** so `nickname.work` and `nickname.family` are distinct slots
  (wishlist §2.1).
- **Domain participates** so a firewall move (health→general, wishlist §2.7) is a genuine
  *re-key* (a new slot), not an in-place mutation — see §3.4 and the Track F flag.

### 3.2 `value_identity` — making "which member" stable (the array fix)

For set-valued predicates we need a stable handle for "this member of the set" that
survives value corrections (you fix a typo in a phone number; it is still *the same*
phone-number slot, now corrected — not an add). Definition, in priority order:

1. **Object identity** for `ref` predicates: `value_identity = object_id`. "Jeff's child
   Summer" is keyed by Summer's entity_id, so re-spelling her name doesn't fork the set.
2. **Natural key** where the value type declares one (e.g. normalized E.164 for
   `phoneNumber`, lowercased domain for `email`).
3. **Minted member id** otherwise: a `gen_random_uuid()` assigned when the member is first
   added, carried forward by every supersession of that member. Stored on the row; the
   *edit operation* references it so "replace head" and "remove" target a member exactly.

This is the crux: **add** mints a new `value_identity`; **replace** reuses the member's
existing `value_identity` (so it supersedes that member); **remove** tombstones that
`value_identity`. The ambiguity disappears because the operation always names the member,
and the key encodes membership.

### 3.3 Live-row selection (current truth, bitemporally)

```sql
-- One partial unique index enforces "≤1 live row per slot" for the believed-now view.
CREATE UNIQUE INDEX one_live_per_slot
  ON fact_assertion (slot_key)
  WHERE tx_to IS NULL AND state = 'live';
-- For set predicates the slot_key already includes value_identity, so this enforces
-- "≤1 live row per member" — exactly the set semantics we want (add ⇒ new key ⇒ allowed).

-- "What do we believe is true NOW, in the world, as of as_of_valid?"
CREATE VIEW fact_current AS
SELECT * FROM fact_assertion
WHERE tx_to IS NULL AND state IN ('live')           -- currently believed
  AND (valid_from IS NULL OR valid_from <= now())    -- valid-time gate (Track G refines)
  AND (valid_to   IS NULL OR valid_to   >  now());
```

Two independent time gates: `tx_to IS NULL` ("we still believe this record") and the
valid-time window ("it is true at the queried instant"). That is the bitemporal split the
invariants require: you can ask "what did we believe on 2026-01-01 about where Sam worked
in 2019" by gating `tx_from/tx_to` on the belief date and `valid_*` on 2019.

### 3.4 Provenance

```sql
CREATE TABLE fact_provenance (
  assertion_id uuid NOT NULL REFERENCES fact_assertion(assertion_id),
  note_id      uuid NOT NULL,                 -- via storage abstraction, not a raw path
  span_start   int, span_end int,             -- char offsets into the note's normalized text
  quote        text,                          -- the cited span (denormalized for audit stability)
  extractor_run uuid,                          -- which extraction/integration run produced it
  source_kind  text NOT NULL,                 -- 'extracted' | 'human_correction' | 'agent'
  PRIMARY KEY (assertion_id, note_id, span_start)
);
```

Provenance is **per-assertion** (immutable), so each revision keeps the exact source that
justified *it*. Correcting provenance (wishlist §2.15) supersedes the assertion with an
identical triple but a corrected span — history shows the old citation was wrong. A
single assertion may cite multiple spans (corroboration) → multiple rows.

### 3.5 Resolution through redirects (split/merge correctness)
`subject_id`/`object_id` are stored as written, but reads resolve through
`entity.redirect_to` (follow the chain to the surviving `active` entity). Merge = set
`redirect_to`; un-merge = clear it. This is why merge is O(1) and reversible without
touching assertions, and why an accidental merge can be fully undone.

---

## 4. Edits → storage mutations (reversible, audited)

Every edit is one row in an **operation log** plus the assertion inserts it caused. The op
log *is* the audit trail and the undo stack (event-sourcing compensation pattern). Track C
owns the operation *algebra*; Track B owns its *persistence and inverses*.

```sql
CREATE TABLE fact_op (
  op_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id    uuid NOT NULL,
  op_kind     text NOT NULL,        -- set_field|relink|retime|add_to_set|replace_member
                                    -- |remove_from_set|supersede|retract|split|merge
                                    -- |entity_split|entity_merge|pin|set_modality|move_domain
  actor       text NOT NULL,        -- 'human:<id>' | 'agent' | 'reprocess'
  target_slot bytea,                -- slot or value_identity the op addresses
  payload     jsonb NOT NULL,       -- the typed correction (Track C contract)
  inverse_of  uuid REFERENCES fact_op(op_id), -- set when this op is an UNDO of another
  batch_id    uuid,                 -- a review session = an atomic, jointly-undoable group
  created_at  timestamptz NOT NULL DEFAULT now()
);
```

Mapping (each is append-only; the inverse is also an op, so undo is symmetric):

| Edit (wishlist) | Storage mutation | Inverse |
|---|---|---|
| set value / predicate / modality / kind (§2.1–2,6,8) | insert revised assertion (same slot_key for functional; for predicate change, new slot_key) `supersedes` old; close old `tx_to` | re-open old, close new |
| relink subject/object (§2.3–4) | insert assertion with new `subject_id`/`object_id`; recompute `slot_key`/`value_identity`; supersede old | supersede back |
| retime valid_from/to/precision/rrule (§2.5) | supersede with new `valid_*`; **tx-time unchanged-in-meaning** (we now believe a different validity) | supersede back |
| move domain (§2.7) | **re-key**: tombstone old-domain slot, insert new-domain slot, linked by `op_id` (Track F gates the cross-firewall move) | reverse the pair |
| add to set (§9) | insert assertion, **new** `value_identity` | tombstone that member |
| replace head/member (§9) | supersede the member's live row (same `value_identity`) | re-open |
| remove from set (§9) | insert `state='tombstone'` row for that `value_identity` | re-open prior live row |
| split fact (§10) | retract source assertion; insert N children, each `op.payload` cites the source | retract children, re-open source |
| merge facts (§10) | retract N sources; insert 1 result citing all | reverse |
| add missing fact (§11) | plain insert (`source_kind='human_correction'`) | retract |
| entity split/merge (§12) | flip `entity.status`/`redirect_to` (§2,§3.5) | flip back |
| drop/retract/supersede (§13) | `state='retracted'` row (misread) vs supersede (real change) — *distinct states*, so "wrong" and "changed" stay separable in history | re-open |
| pin / confidence (§14) | supersede with `pinned=true` / new `confidence` | supersede back |
| fix provenance (§15) | supersede with corrected `fact_provenance` | supersede back |

**Atomicity & batch undo:** a review session shares one `batch_id`; undoing the batch
re-opens every assertion it closed and tombstones every one it inserted, in one
transaction. Because closure is `tx_to` timestamps (not deletes), undo is loss-free.

**Reprocessing vs. pins:** re-analysis runs as ops with `actor='reprocess'`. A `pinned`
live row cannot be superseded by a reprocess op (only by a human/agent op), satisfying
wishlist §2.14 (reprocessing can't drop an approved fact) at the storage layer.

---

## 5. Rationale

- **Append-only is the only model that makes audit, bitemporality, and undo the *same*
  mechanism.** SCD-2 / event-sourcing convergent practice: never mutate content; close and
  re-open intervals. The op log is simultaneously the change feed, the audit record, and
  the undo stack — no second bookkeeping system to drift.
- **Cardinality-in-the-key** turns the framing's hardest UX problem into a pure data
  property. There is no `if functional then override else add` branch to get wrong; the
  same insert path yields override or add purely from whether `value_identity` is in the
  key. This is the single most important decision in the brief.
- **`value_identity` separated from `value_json`** lets a *correction to a member's value*
  (typo fix) be a supersession of *that* member, not a spurious add — which a naive
  one-edge-per-value scheme gets wrong.
- **Slot as a derived key, not a table,** avoids a second source of truth that can
  disagree with the assertions. Materialization (§5.4) is an optional cache, never the
  authority.
- **Redirect-based split/merge** keeps the two scariest, least-reversible operations O(1)
  and undoable.

---

## 6. Positions on §3 / §4 tensions

### 6.1 Machine-written-wiki doctrine (#7) — clear position
**Structured field edits are modeled as machine-applied correction operations and do NOT
require a doctrine change.** A human never writes a fact row; they emit a typed `fact_op`
(the correction channel). The engine validates it (against the predicate registry, value
shapes, and firewall rules) and *machine-writes* the resulting assertions with
`source_kind='human_correction'` / `actor='human:<id>'`. The wiki remains machine-written;
the human's input is a structured, audited, reversible *instruction*, not prose and not a
direct row edit. The doctrine is preserved precisely because the op log makes every human
intent traceable and unwindable. **Red-team target:** is a fully-editable structured op
"meaningfully different" from a direct edit? Our answer: yes — it is validated, typed,
reversible, and never bypasses canonicalization/firewall checks, which a prose or row edit
would.

### 6.2 One unified shape vs. stage-specific (§3)
Storage takes a **distinct shape with an explicit mapping**: the persisted `fact_assertion`
is *narrower and stricter* than what the LLM emits (Track A). Extraction emits prose-ish
candidates; integration resolves entities/predicates/time; only the resolved, typed,
keyed result is storable. Forcing the LLM to emit storage rows would couple model
reliability to schema rigidity. Mapping is one-way and lossless-upward (provenance keeps
the original span).

### 6.3 Arrays vs. one-edge-per-value (§3) — **one-edge-per-value, at storage.**
Arrays are an *IR/review* convenience (Track A/E may present a set as an array), but they
**must** lower to one assertion per member here, or add-vs-replace becomes ambiguous again.
The slot_key + value_identity is the contract that makes the array presentation safe.

### 6.4 Bitemporal columns (§4) — kept, two independent axes (§3, §3.3). Valid-time
precision/recurrence semantics are Track G's; Track B reserves the columns and the
live-row gates. Transaction time = interval closure on supersede (`tx_from`/`tx_to`);
`reported_at` separately captures the *source's* claim time, which is neither valid nor
system time (e.g. a note written today about a job held in 2019).

### 6.5 RLS / firewalls (§4) — note, defer depth to Track F
Every table carries `owner_id` and rows carry `domain_code`; RLS policies scope by owner
and firewall band on `fact_assertion`, `entity`, `fact_provenance`, `fact_op`. **The two
storage-specific hazards Track F must own:** (a) a **relink** whose object is in a
firewalled domain, and (b) a **domain move** (§3.4) — both are modeled as *re-keys across
a firewall boundary* and must be policy-gated, not silent. The op log itself is
domain-scoped so a health correction is not visible in a general-domain audit view. Each
new table here ships an RLS isolation test per CLAUDE.md rule 3.

---

## 7. Tradeoffs & alternatives

- **A. Single mutable "current" table + history side-table** (classic SCD-2). Rejected:
  two tables can disagree; "current" updates are not reversible without the side-table
  being perfect; bitemporal queries get awkward. Our append-only single table with a
  partial live-index gives "current" as a view for free.
- **B. RDF-style triple store with reification for metadata.** Rejected: reification
  explodes row count and makes modality/time/provenance second-class. Property-graph edge
  rows carry metadata natively (the framing's stated need).
- **C. Arrays-in-a-column for set-valued predicates.** Rejected outright — this *is* the
  override-vs-array bug. You cannot supersede or tombstone one array element with clean
  audit/provenance; concurrent edits clobber. One-edge-per-value is non-negotiable.
- **D. Materialize the slot as a table** (`fact_slot` holding the live pointer). Optional
  optimization (§5.4) if the partial-index live-view is too slow at scale; kept as a
  cache rebuildable from assertions, never authoritative.
- **E. Store cardinality only in the registry, not on the row.** Rejected: the row
  snapshots `cardinality` at write time so a *later* registry flip (functional↔set) does
  not silently re-interpret old rows' identity. A cardinality change becomes an explicit
  migration op, not a quiet semantic shift.
- **F. Hard-delete on retract.** Rejected — breaks audit & reversibility (§4).

---

## 8. Risks / failure modes

1. **Predicate cardinality flips after data exists.** If `nickname` was functional and
   becomes set-valued, old slot keys (no value_identity) and new ones diverge → a person
   could appear to have a "lost" nickname. *Mitigation:* cardinality is row-snapshotted
   (§7-E); flips are an explicit re-key migration op, eval-gated, reversible.
2. **`value_identity` instability** for set members lacking a natural key. If the minted
   id isn't carried forward correctly, a value correction forks into an add (the original
   bug, re-introduced). *Mitigation:* the *edit op* always references the member's existing
   `value_identity`; only `add_to_set` mints. Strong test obligation.
3. **Slot-key drift from predicate canonicalization.** If a predicate is re-canonicalized
   (Track A / PREDICATE_CANONICALIZATION), the slot_key changes and old + new assertions
   stop grouping. *Mitigation:* canonicalize *before* keying; a consolidation rewrite
   (already exists per the predicate doc) re-keys stored drift rows as a tracked op.
4. **Live-uniqueness races** under concurrent ingest + human edit on the same slot. The
   partial unique index enforces ≤1 live row but two concurrent inserts both closing the
   same old row can deadlock or double-supersede. *Mitigation:* serialize per-slot writes
   (advisory lock on slot_key) inside the op transaction.
5. **Bitemporal interval gaps/overlaps** in valid-time (two live employers with
   overlapping `valid_*` when the predicate is functional-over-time). This is Track G's
   semantics, but storage must not *enforce* non-overlap for set predicates while it
   *should* flag it for functional-over-time ones. Coordinate.
6. **Redirect chains** (merge A→B, later B→C) growing/looping. *Mitigation:* path-compress
   on read; forbid cycles via a check; cap depth.
7. **Op-log + assertion divergence** if an op is recorded but its inserts fail (or vice
   versa). *Mitigation:* one transaction per op; the op row and its assertions commit
   together or not at all.
8. **Firewall leak via relink/move** (§6.5) — deferred to Track F but flagged as the
   highest-severity storage-adjacent risk.

---

## 9. Open questions for the red-team

1. **Functional-over-time vs. functional-now.** `currentEmployer` is functional *now* but
   the *history* is a sequence — is that a functional predicate with valid-time
   supersession, or a set predicate filtered to "live now"? We lean *functional with a
   temporal succession of supersessions*; Track G should rule. Get this wrong and "former
   employer" history is malformed.
2. **Is `value_identity` for natural-keyed set members worth the complexity,** or should
   *every* set member always get a minted id (uniform, but a phone-number typo then can't
   be auto-recognized as "the same member corrected")? Tradeoff: uniformity vs. dedup.
3. **Slot materialization threshold.** At what corpus size does the partial-index live-view
   stop being fast enough, forcing §5.4's `fact_slot` cache? Needs a perf model (Track —
   performance lens).
4. **Should domain-move be a re-key (new slot) or a same-slot attribute change?** We chose
   re-key so the firewall boundary is crossed explicitly and auditably; the cost is that
   the moved fact's slot identity changes (downstream references must follow the op link).
   Red-team: does any consumer rely on slot-key stability across a domain move?
5. **Granularity of "the same fact."** Does `qualifier` always belong in the slot key? Edge
   cases: is `nickname.work` editing the audience a *retime/relabel* of one slot or a
   *move* between two slots? We treat qualifier as keyed (→ move); confirm with Track C's
   algebra.
6. **Provenance immutability vs. multi-note corroboration.** When a second note corroborates
   an existing live assertion, do we (a) add a provenance row to the *same* assertion
   (mutating an "immutable" row's child table) or (b) supersede with merged provenance? We
   lean (a) — provenance is additive evidence, the assertion content is unchanged — but
   that nuances the "assertions are immutable" claim. Red-team the audit implications.
7. **Reported-time vs. transaction-time conflation.** Is a separate `reported_at` column
   the right home for "the note's own claim time," or does that belong to the *note*, not
   the assertion? Coordinate Track A/G.
```