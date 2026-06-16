# Red-team R1 — Migration, back-compat & reversibility

**Lens:** MIGRATION, BACK-COMPAT & REVERSIBILITY. Adversarial.
**Target:** `20-spec-v0.md` (+ `00-framing.md` §4 invariants; tracks B, C, D consulted).
**Thesis under attack:** "every op has a precomputed inverse / the op-log *is* the
undo stack," "budgeted re-analysis is a planned migration, never silent drift,"
and the implicit claim that the existing corpus migrates cleanly into the new model.
**Method:** construct concrete interleavings that break invertibility, idempotency,
and migratability; grade Sev-1 (breaks an invariant or core goal) … Sev-3 (nit).

---

## SEV-1 findings

### M1 (SEV-1) — "Precomputed inverse" is a lie under op interleaving: the stored inverse undoes the *wrong* state

**Claim attacked:** §1 spine #2 and §8 — "the op-log *is* the undo stack," each op
carries "a precomputed inverse" (C §2.5, B §4). The spec presents
`op : GraphState → (GraphState, AuditRecord, InverseOp)` as if the inverse is total.

**Why it breaks:** the inverse is precomputed at *apply* time from `preconditions`
(the RFC-6902 `test` discipline, C §2.2). RFC-6902 `test` gives you *detection* of a
changed base, not *reversibility* of an interleaved history. The op-log is a **total
order** (ULID, C §2.2) but undo is requested **out of order**. The stored inverse of
op *k* is only valid if the live state still equals `target_after(k)`. Once op *k+1*
has further mutated that slot, replaying inverse(*k*) reconstructs `target_before(k)`
— which is **not** "remove the effect of *k* from current state."

**Concrete interleaving:**
1. `op1 = set_field value` on functional fact F: A1c `5.4 → 5.8`. Stored inverse:
   "set value 5.8 → 5.4." `target_after = 5.8`.
2. `op2 = set_field value` on F: `5.8 → 6.1`. Stored inverse: "6.1 → 5.4"? No — its
   precondition snapshot is `5.8`, so its inverse is "set 6.1 → 5.8."
3. User undoes **op1** (not op2). The stored inverse says "set value → 5.4". The
   precondition `test value == 5.8` **fails** (live is 6.1). Per C §6 risk 7, a
   precondition mismatch **rejects** the undo. So op1 is now *un-undoable* without
   first undoing op2 — but nothing in the spec says undo must be LIFO, and the review
   UX (E, batch undo) actively invites undoing an arbitrary earlier batch.

The op-log is therefore **not** a free undo stack. It is an undo *stack* only if undo
is strictly LIFO. The spec never states a LIFO constraint, and §8's "batch undo via
`batch_id`" explicitly undoes a *named historical batch*, not the top of the stack.
Selective mid-history undo requires either (a) a true inverse-rebase (recompute the
inverse against current state, which the precondition discipline forbids) or (b)
rejecting the undo (which violates the "every committed change is unwindable"
invariant, framing §4).

**Severity rationale:** directly breaks the binding invariant "every committed change
is … unwindable (reopen/undo)" (§4) for the common case of correcting an earlier
mistake after later edits. The spec markets this as solved; it is not.

**Fix:** Drop the "precomputed inverse is the undo" framing and adopt **inverse
*recompilation* against current state**, gated by a per-slot dependency check:
- Undo of op *k* is legal iff no later live op *depends on* op *k*'s output
  (same `slot_key`/`value_identity`/entity). Maintain an explicit
  **undo-dependency edge** (C open-Q 8 already smells this) so undo is "revert this
  op and everything causally after it on the same target," presented as a preview.
- Where the user truly wants "remove op *k*'s delta but keep op *k+1*," that is a
  **new forward correction**, not an undo — and must be labelled as such, not sold as
  reversibility. The invariant is satisfied by *cascading* undo (undo k+1 then k), not
  by a magic standalone inverse.

---

### M2 (SEV-1) — Re-analysis under a new contract resurrects retracted/removed facts (idempotency failure)

**Claim attacked:** D §4.2 "re-extraction is reproducible (E3) and diffable, … blast
radius is computable before it runs"; spine #2 "no second bookkeeping system to
drift." The hidden assumption: re-extraction produces *facts*, and the diff against
the live graph is well-defined.

**Why it breaks:** re-extraction re-reads the *note*, not the *op-log*. Human
**retractions and removals are op-log state, not note content.** A note still contains
"my employer is Acme"; the human retracted the extracted fact as a misread (the source
sentence was sarcasm, or the entity was the wrong Acme). On major-version re-analysis,
the extractor re-emits the Acme fact from the unchanged note. The shadow-diff (D §4.2
step 3) sees "new fact not in old set" and — per the auto-accept rule "new ⊇ old with
no semantic loss" — **commits it**. The retraction is silently undone.

**Concrete interleaving (the resurrection):**
1. Extractor v3 emits fact F (`Sam works_for Acme`) from note N.
2. Human `retract{reason: misread}` F. F is `state='retracted'`, queryable, newest-wins
   ignores it (C Group E). The op-log records the retraction; the note is unchanged.
3. Contract bumps to v4 (major). Re-analysis re-extracts N. The model, reading the same
   prose, emits F' (`Sam works_for Acme`), a *new* `claim_id`/`assertion_id`.
4. Diff: F' has no live predecessor (F is retracted, not live). Auto-accept path treats
   F' as net-new. **F' commits live.** The retraction is dead.

C §6 risk 4 anticipates a *narrower* version of this (set-member resurrection across
re-mint) and waves at "pinned and human-touched members are protected" — but **a
retraction is not a pin.** Nothing in the spec says a retracted fact's *identity*
suppresses future re-extraction of the same claim. The protection (D §4.2 step 3) is
explicitly scoped to "previously human-**pinned**" values only.

**Severity rationale:** silently re-introduces facts the human explicitly killed —
exactly the "reprocessing can't drop an approved fact" guarantee (wishlist §14) running
in reverse, and a direct violation of "never silent drift." For health/finance facts a
resurrected retraction is also a firewall-relevant correctness failure.

**Fix:** Retractions and removals must be **first-class suppression state keyed on the
slot/claim identity, consulted by re-extraction**, not just lifecycle flags on a row:
- Define a **`suppression` ledger** (or reuse the op-log as the authority — see Position
  P2): on re-extract, a newly-emitted claim whose `slot_key` (+ `value_identity` for
  sets) matches a *human retraction/removal that post-dates the most recent supporting
  note edit* is **routed to review, never auto-accepted**, carrying "you retracted this;
  re-extraction re-proposed it."
- This requires the diff in D §4.2 to be a **3-way diff** (old machine facts ⊕ human op
  overlay vs. new machine facts), not a 2-way fact diff. The spec's 2-way diff is the
  bug.

---

### M3 (SEV-1) — Pinned/human-edited facts under a new contract: the pin can't carry the new shape, so it either blocks the migration or is silently re-shaped

**Claim attacked:** D §4.2 "Pinned facts (wishlist §14) are immutable to migration
unless explicitly reviewed"; §5 success "versioned and migratable." D open-Q 6 already
flags the tension but the spec takes no resolved position.

**Why it breaks:** "immutable to migration" and "migrate the contract" are in direct
contradiction for a **major** version bump, which by definition changes the *shape* a
fact must have (new typed value variant, new required `temporal` sub-field, split of a
predicate). A pinned v3 fact is frozen in the v3 shape. After cutover to v4, the live
graph contains rows in two incompatible shapes. Three bad outcomes, none chosen:
- **(a)** Readers must handle both shapes forever (D §4.1 says "old + new coexist …
  until cutover completes" — but pinned facts make cutover *never complete*). The
  "single pinned `contract_version`" invariant (A3 hard-rejects non-active shapes) now
  has permanent exceptions living in the live table. A3 will **reject reads/re-validation
  of pinned v3 facts** under the v4 validator.
- **(b)** Force the pinned fact through the migration anyway → silently re-shapes a
  human-approved fact → violates wishlist §14 and #7 (machine re-authoring human-pinned
  content).
- **(c)** Block the major migration until every pinned fact is hand-reviewed → a single
  pin can stall a corpus-wide migration indefinitely (no SLA, no bound).

A human who did a `set_field`/`replace_head`/`retime` (without pinning) is *worse off*:
their edit isn't pin-protected at all (protection is "pinned only," D §4.2 step 3), so a
human-corrected-but-unpinned fact is **auto-re-extracted and may be overwritten** by the
v4 model — the human's correction silently lost.

**Concrete scenario:**
1. Human corrects F's `valid_to` via `retime` (marks "former, ended 2024"). Does not pin.
2. v4 major bump changes the temporal sub-shape (G `g-temporal/2`: new `bound` value).
3. Re-analysis re-extracts F from the note ("used to work at Acme"), model emits
   `bound:unknown` (no date in prose). Diff: new temporal ≠ old temporal, but neither is
   pinned → not in the protected set → **auto-accepted under "new ⊇ old"?** The human's
   "ended 2024" is *not* in the note, so "new ⊇ old with no semantic loss" is false — but
   the spec's loss-detection is per-field *machine* comparison; it has no concept of
   "this field was human-authored and the note can't reproduce it." It overwrites.

**Severity rationale:** silently discards human corrections on re-analysis — the exact
failure the redesign exists to prevent, and a #7 + wishlist-§14 violation.

**Fix:**
- **Protect human-touched, not just pinned.** Any field that carries an op-log entry with
  `actor='human:*'` since the last note edit is **frozen against re-extraction** and any
  conflicting re-extracted value routes to review. "Pinned" becomes "explicitly
  protected"; "human-edited" becomes "implicitly protected." (This is what wishlist §14
  *means*; the spec under-scoped it to the literal `pin` flag.)
- **Major migration must define a per-pin upgrade path:** a pinned v3 fact is migrated by
  a **deterministic shape-lift** where one exists (minor-style backfill), else it is
  *explicitly enumerated as a migration blocker* with a bounded review queue and a
  visible count — never silently re-shaped, never silently left to break A3. Cutover
  completes when blockers = 0, and the blocker count is the migration's SLA surface.

---

## SEV-2 findings

### M4 (SEV-2) — Domain-move (downgrade) reversibility laundering survives the copy-forward audit (§7(f))

**Claim attacked:** §7(f) provisional pick (ii): downgrade is an owner-only copy-forward,
"reversible by retracting the general copy, with downstream cites flagged on undo." The
spec asserts the copy-forward + both-domain audit defeats laundering.

**Why it breaks — the move→retime→undo launder:**
1. Health fact H (`A1c 9.1`, domain=health). Owner issues `move_domain` downgrade →
   mints general copy G (re-derivation, cites H, H marked `superseded`-in-health). Audit
   in both domains. G is now general-visible.
2. A downstream general-domain consumer (wiki render, search index, an *agent*) reads G
   and **emits a derived general fact D** ("elevated marker noted") citing G. D is a
   first-class general fact with its own provenance to G — **no longer pointing at any
   health row.**
3. Owner `retract`s G (the "reversible" undo of the downgrade). Audit flags G's
   downstream cites for "re-evaluation" (§7(f), C risk 2). But **D already exists**, was
   derived while G was live, and cites G — not H. Retracting G does not retract D; D is
   general-domain content semantically derived from health data. The health value has
   **escaped the firewall and survived the undo.** The "flag for re-evaluation" is a TODO,
   not an enforcement: nothing *retracts* D, and a general-domain reader cannot even see
   that D's lineage traces to a health row (F's whole point: general readers never
   resolve health rows).

The copy-forward makes the *move* auditable but does **not** make the *information flow*
reversible. Reversibility of a firewall crossing is a flow property, not a row property;
the spec conflates the two. F open-Q 3 names exactly this and the spec's pick (ii) does
**not** close it — it relies on a flag whose enforcement is undefined.

**Severity rationale:** a reversibility claim that doesn't hold for the one op where
reversibility is firewall-critical. Not Sev-1 only because it requires a downstream
derivation step to occur while G is live and is bounded by the owner-only, rate-limited,
non-batchable gates (F §3) — but it defeats the stated "reversible" property and is a
laundering channel.

**Fix:** Adopt §7(f) flip-condition **(iii): downgrade is one-way.** Once a health value
is copied forward to general and *any* general-domain read/derivation occurs, undo cannot
restore the firewall (the information has flowed). Model downgrade as **irreversible by
construction**: re-protecting requires authoring a *new* fact, and the general copy, once
read, can only be *retracted-going-forward* (stops new reads) — never "un-leaked."
Alternatively, gate downstream derivation: a general fact derived from a copy-forward G
inherits a **taint marker** and is transitively retracted when G is retracted (a
cascade), but this re-introduces cross-domain lineage tracking that F's projection model
was designed to forbid — so **(iii) one-way is the clean answer.**

---

### M5 (SEV-2) — Entity-merge undo is unsafe once downstream facts attach to the survivor

**Claim attacked:** B §2 / C Group F — `merge_entities` is "O(1) and trivially
reversible (clear `redirect_to`)" / non-destructive merge_link, "unmerge is just deleting
the link." §3.3 provisional adopts F's projection model but keeps B's O(1)
redirect-within-domain.

**Why it breaks:** the merge is reversible *only if the world is frozen between merge and
unmerge.* After merge A→B (survivor B), new facts get authored **against the survivor's
identity**. Unmerge must now answer: which of B's facts belonged to A, which to B, which
were authored *post-merge* (and belong to neither cleanly)? `redirect_to`-clear restores
A and B's *original* facts, but a fact authored after the merge — e.g. extractor reads a
new note "Sam got promoted," resolves "Sam" to the merged survivor B, attaches
`works_for.title` to B — has **no recorded origin assignment** to A or B. On unmerge it
defaults to staying on B (the survivor surrogate). If the *real* Sam in that note was the
A-identity, the fact is now mis-attributed and the unmerge silently moved it.

**Concrete interleaving:**
1. `merge_entities{sources:[A,B], survivor:B}`. Facts re-resolve B.
2. Re-analysis / new note: `add` fact P (`title=Director`) resolves subject → B (the only
   live identity for "Sam").
3. `merge_facts` later combines P with an older B-fact into one (§4 structure op), so P's
   identity is now fused.
4. `unmerge_entities` (the "trivial" inverse). A and B re-expose. **Where does P go?** It
   was authored against B and fused; the merge audit (C §2.5 attribute-level survivorship)
   recorded the *merge's* survivorship, not P's *post-merge provenance to a pre-merge
   identity*. Unmerge cannot place P deterministically.

C §2.3 `split_entity` "requires an explicit per-fact assignment (no heuristic)" —
correct — but `unmerge_entities` is sold as the *cheap* inverse with **no** such
assignment. The two cannot both be true: a faithful unmerge after downstream attachment
*is* a split, requiring per-fact assignment. The spec's "clear the link" unmerge is only
valid with zero post-merge writes.

**Severity rationale:** an identity op advertised as O(1)-reversible is in fact only
reversible under a no-write window; in steady state it silently mis-attributes
post-merge facts. Sev-2 (not Sev-1) because it corrupts attribution, not firewall
isolation, and is detectable via audit — but the "trivially reversible" claim is false.

**Fix:** Make `unmerge_entities` carry the **same explicit per-fact assignment contract as
`split_entity`** for any fact authored or mutated after the merge timestamp. Cheap
link-clear is permitted **only** when `audit` proves no `fact_op` touched the survivor's
slots between merge and unmerge (a verifiable precondition). Otherwise unmerge degrades to
a reviewed split with an assignment map. State this precondition explicitly; do not market
unmerge as unconditionally trivial.

### M6 (SEV-2) — Split↔merge inverses are not clean inverses after the children diverge

**Claim attacked:** C §2.4 — `split_fact` undo is `merge_facts`; `merge_facts` undo is
`split_fact` (presented as a clean inverse pair). §7(b) assumes per-cell ops serialize
cleanly.

**Why it breaks:** `split_fact(F) → {c1, c2, c3}` then the children **diverge**: `c2` gets
`relink_object`'d, `c3` gets `retime`'d, `c1` gets `retract`'d. Now "undo the split" via
`merge_facts({c1,c2,c3} → F)` is **not** the inverse — it would have to merge a retracted
child, a relinked child, and a retimed child back into the original F, discarding their
post-split history or fabricating a merged value that never existed. The stored inverse
(C §2.5, computed *at split time*) is `merge_facts` over the *then-current* children; it
is stale the moment any child is independently edited. This is C open-Q 8 ("does undoing a
split_fact whose children were later relink_object'd compose correctly?") — and the answer
the spec needs to admit is **no, not as a standalone inverse.**

Symmetrically, `merge_facts({a,b}→m)` then `m` is edited: undo `split_fact(m)→{a,b}` must
restore a and b from the *merge-time snapshot* (C says "from the audit snapshot"), which
**discards the post-merge edit to m** silently — a lost human correction if the edit was
human.

**Severity rationale:** the headline "every structure op has a clean inverse" is false for
the realistic case (post-structure-op edits). Sev-2: corrupts/loses edits on undo but is
contained to the split/merge family and detectable via the dependency check M1's fix
introduces.

**Fix:** Same as M1 — undo of a structure op is **only** legal when no later live op
depends on its outputs (the children for split, the merged row for merge). Enforce via the
undo-dependency graph; otherwise the structure-op undo is blocked and the user must
cascade-undo the children's edits first (presented as a preview). The inverse stored at
apply time is a *hint for the no-dependency fast path*, not a guarantee.

### M7 (SEV-2) — Migrating the existing corpus loses cardinality intent, member identity, and bitemporal split; "migratable" is asserted but unspecified

**Claim attacked:** §5 success "the LLM contract is … migratable"; §8 "spec stays
buildable." The spec is greenfield and **nowhere specifies the migration of *today's*
facts** into `fact_assertion` + `slot_key` + `value_identity` + bound-trichotomy temporal.

**Why it breaks — concrete losses migrating the existing graph:**
1. **Cardinality snapshot can't be reconstructed.** B §7-E snapshots `cardinality` on the
   row "so a later registry flip doesn't re-interpret old rows." But existing facts have
   **no such snapshot** — they predate the registry `functional` column existing per-row.
   Migration must *stamp* cardinality from *today's* registry. If a predicate's
   functional/set status is later judged to have been wrong historically, every migrated
   row is mis-keyed and the §8-E protection is defeated retroactively. The migration *is*
   the silent re-interpretation the design claims to prevent.
2. **`value_identity` for legacy set members is unrecoverable.** Existing set-valued facts
   (multiple employers, phone numbers) have no stable member id. B §3.2 priority 3 mints
   one "when the member is first added" — but on migration, *every* legacy member is "first
   added simultaneously," and a later typo-correction note that the old system already
   merged into one value will, post-migration, look like a member that was always singular.
   Worse: if the legacy store *did* silently replace heads (the original bug!), the lost
   history can't be reconstructed — migration faithfully imports the bug's output as if it
   were intended set state.
3. **Bound trichotomy can't be inferred from legacy `valid_to`.** The legacy model (B §3
   research shape) has `valid_to timestamptz` (NULL=ongoing). The new model needs
   `valid_to_bound ∈ {closed, open, unknown}` — the *whole point* of the `—→2026` fix. A
   legacy `valid_to=NULL` is **ambiguous**: is it "ongoing" (`open`) or "former without a
   date" (`unknown`)? The legacy data **does not distinguish these** (that's the bug being
   fixed). Migration must pick one for every legacy fact, and either choice is wrong for a
   large fraction — silently fabricating "ongoing" for ended-without-date facts or vice
   versa.

**Severity rationale:** "migratable" is a stated success criterion and load-bearing for
adoption; the spec asserts it without a single migration mapping, and the three losses
above are not cosmetic — they re-introduce the exact bugs the redesign targets. Sev-2
(not Sev-1) only because it's a build-time, one-time, reviewable migration rather than a
steady-state firewall break — but it must be designed, not assumed.

**Fix:** Add a **corpus-migration spec** to the final doc with explicit per-field mappings
and an **honest "ambiguous → review/inferred" policy**:
- Cardinality: stamp from today's registry, but record `cardinality_source='migration'`
  so a later correction is a tracked re-key op, not a silent flip.
- Member identity: mint `value_identity` per legacy member; **flag the whole migrated set
  as `provenance.source_kind='migrated'` + low-confidence** so a later note can re-anchor
  members rather than fork them; accept that pre-migration silently-replaced history is
  unrecoverable and say so.
- Bound: legacy `valid_to=NULL` migrates to `bound='unknown'` **only** when status was
  "former"-flagged; otherwise `open`; where the legacy data can't tell, **migrate to
  `unknown` + route to review** (conservative: don't fabricate "ongoing"). Document the
  heuristic and its error rate.

### M8 (SEV-2) — Op-log replay-from-genesis is claimed as the audit/undo foundation but is non-replayable across contract+registry versions, and grows unbounded

**Claim attacked:** C §2.5 "Replaying the op-log from genesis reconstructs the graph
(event-sourcing guarantee), so audit + reversibility are structural"; spine #2 "the
op-log *is* the change feed, the audit trail, and the undo stack." C §5 hedges to "we
don't *require* replay for normal operation," but §2.5 and the spec's invariant table
(§8) lean on the genesis-replay guarantee for the reversibility invariant.

**Why it breaks:**
1. **Ops are not self-contained — they depend on external mutable state.** `set_field
   predicate` calls "the predicate-canon path" (C §2.3); `relink` resolves entity
   candidates; cardinality is stamped "from the registry." Replaying op *k* from genesis
   re-executes these against **today's** registry/canonicalizer/embedding model, not the
   version that was live when op *k* applied. The replay is **not deterministic across
   registry evolution** — the predicate registry, entity-resolution embeddings, and value
   parsers (D B2) all drift. Event sourcing's replay guarantee requires *pure* events;
   these ops are impure (they invoke versioned services). So genesis-replay does **not**
   reconstruct the historical graph — it reconstructs *what those ops would do today*,
   which differs. The audit/undo invariant cannot actually rest on genesis replay.
2. **`schema_version` skew on stored inverses (C §6 risk 6) compounds this.** A v2 inverse
   op replayed under a v4 processor is "re-validated, not blindly applied" — meaning the
   undo *may now be rejected* by a stricter v4 validator. So even the *stored* inverse is
   not guaranteed replayable, breaking the "every change is unwindable" invariant for
   long-lived facts across a major bump.
3. **Unbounded growth + replay cost.** The op-log is the audit trail *and* (per §2.5) the
   reconstruction substrate, so it can never be truncated without losing the genesis-replay
   guarantee. For a personal KB over years this is bounded-ish, but "reconstruct by replay"
   becomes O(all history) for any forensic query, and the op-log carries `target_before`/
   `target_after` full snapshots (C §2.5) — i.e. it's not a compact event log, it's a
   full-snapshot journal that grows with edit volume × fact size.

**Severity rationale:** the spec *names* genesis-replay as the structural basis for the
reversibility invariant, and it does not hold across the versioning the same spec
mandates. Sev-2 because C §5's pragmatic fallback ("graph is live, op-log is authoritative
history, replay only for forensics") already partly retreats — but the spec must *commit*
to that retreat and stop claiming genesis-replay, or the audit invariant is built on sand.

**Fix:**
- **Make ops pure for replay:** every op that invokes a versioned service must **freeze its
  resolved output into the payload** (the canonical predicate id chosen, the entity id
  resolved, the parsed typed value, the cardinality stamp) so replay re-applies the
  *recorded outcome*, never re-invokes today's services. Record the
  `extractor/prompt/validator/registry` 4-tuple (D §4.1) **on every op**, not just on
  extracted facts.
- **Do not rest the invariant on genesis-replay.** Commit to C §5's position
  (graph-live + op-log-as-history + snapshot-checkpoints); replay is for forensic
  reconstruction *to a snapshot boundary*, not from genesis. Periodic **immutable
  checkpoints** of the materialized graph bound replay cost and let the op-log be
  archived (not truncated) behind a checkpoint.

---

## SEV-3 findings (nits / watch-items)

- **M9 (SEV-3) — `add_fact` undo orphans an auto-drafted correction note.** §7(e) pick (i)
  auto-drafts a correction note when a human `add_fact`s. The undo of `add_fact` is
  `retract` (C Group D) — which retracts the *fact* but the spec says nothing about the
  *drafted note*, which may already have flowed through ingestion (#7 loop) and influenced
  the wiki. Fix: `add_fact` undo must also retract/supersede its drafted note, or the note
  must be marked provisional-until-fact-confirmed.

- **M10 (SEV-3) — `recorded_at`/`tx_from` defaulting to `now()` makes migrated facts
  un-time-travelable.** B §3 columns default `recorded_at`/`tx_from` to `now()`. Bulk
  migration stamps every legacy fact with the *migration* timestamp, collapsing the
  historical transaction-time axis — "what did we believe on 2025-01-01" returns nothing
  before migration day. Fix: migration must backfill `tx_from`/`recorded_at` from legacy
  capture timestamps, not `now()`.

- **M11 (SEV-3) — batch undo (`batch_id`) interacts with M1.** "Undo the whole review
  batch" (§8) re-opens every assertion the batch closed — but if a *later* batch touched
  one of those slots, batch-undo hits the same stale-inverse wall as M1, partially. The
  dependency-graph fix (M1) must operate at batch granularity too.

---

## Positions on assigned open conflicts

### P1 — §7(f) domain-move reversibility / laundering: **adopt (iii) one-way downgrade.**

The provisional (ii) copy-forward is auditable but **not reversible in the property that
matters** (information flow across the firewall), as M4 demonstrates: once a general
consumer derives from the general copy, retracting the copy does not un-leak the value, and
the "flag downstream cites for re-evaluation" is an unenforced TODO. Reversibility of a
firewall crossing is a *flow* property; the copy-forward only makes the *row* reversible.
Since the RLS firewall invariant is binding and non-negotiable, the conservative correct
position is: **a downgrade is one-way.** Re-protection requires authoring a new fact;
the general copy can be retracted *going forward* (no new reads) but is never treated as
"un-leaked." This also kills the move→retime→undo laundering class (the §7(f) flip
condition itself names this as the trigger for (iii)). Keep the owner-only,
non-batchable, copy-forward *mechanics* of (ii) — they're good — but **drop the
reversibility claim** and label downgrade irreversible.

### P2 — Op-log as source of truth vs. materialized graph (replay-from-genesis necessity): **materialized graph is the live source of truth; op-log is the authoritative, append-only change history with periodic immutable checkpoints. Genesis-replay is NOT required and should be removed as a stated guarantee.**

Reasons, from M8: (1) the ops are **impure** — they invoke the predicate registry, entity
resolver, and value parsers, all of which version and drift — so genesis-replay does not
reproduce the historical graph; it reproduces "what those ops do today." (2) Stored
inverses can be rejected by a future validator (schema skew), so the inverse is not an
unconditional undo. (3) Full-snapshot audit rows make the log a journal, not a compact
event stream, so genesis-replay is also costly. The clean position (already half-admitted
in C §5) is **graph-live + op-log-as-history + checkpoints**. To make undo and forensic
replay sound, **ops must record their resolved outputs and their pipeline 4-tuple**, so
replaying re-applies recorded outcomes rather than re-deriving them. The op-log remains the
single audit/undo mechanism — but its guarantee is "replay *to the nearest checkpoint*
reconstructs the graph using *recorded* (not re-derived) outcomes," which is the only
version that actually holds. The spec's current "single mechanism, no second bookkeeping
to drift" survives; only the over-strong "genesis-replay reconstructs the graph" claim is
dropped, and the dependency-graph for selective undo (M1/M6) is added.

---

## Summary of what must change in the spec

1. **Reversibility is conditional, not total** (M1, M6, M11): add an explicit
   undo-dependency graph; undo of an op is legal only with no live dependents, else it
   cascades. Stop selling "precomputed inverse = free undo stack."
2. **Re-analysis must be a 3-way diff** (machine-old ⊕ human-overlay vs machine-new), and
   must protect **human-touched**, not just **pinned**, fields; retractions/removals
   suppress re-extraction of the same claim (M2, M3).
3. **Domain-move downgrade is one-way** (M4, P1).
4. **Entity unmerge after downstream writes is a reviewed split**, not a link-clear (M5).
5. **Add a corpus-migration spec** with explicit, honest "ambiguous → review/inferred"
   mappings for cardinality, member identity, bound trichotomy, and transaction-time
   backfill (M7, M10).
6. **Drop genesis-replay as the invariant's basis; freeze resolved outputs into ops; add
   checkpoints** (M8, P2).
