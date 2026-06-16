# Red-team R2 — Performance & Scale lens

**Spec under attack:** `21-spec-v1.md` (revised after R1).
**Lens:** performance & cost at realistic scale. Same target as R1: ~10 years of
notes, **10^4–10^5 live facts**, **10^5–10^6 assertion rows** (live × avg-revisions),
thousands of entities with up to 4 domain projections, hundreds of recurring facts.
**Method (per R1):** trace each hot path; give Big-O / row-count / round-trip cost;
name where it falls over and the specific index/materialization/cache that saves it.
**Dominant cost model carried from R1:** single-owner, so the whole corpus sits behind
one `owner_id`; selectivity must come from `(subject, predicate, domain, value_identity)`,
never `owner_id`/`domain_code` alone (those partition 10^6 rows into ~4 buckets).

This round does two things: **(Part A)** verifies that v1 actually closed each R1 perf
finding (not just asserted it), and **(Part B)** attacks the *new* costs the R1 fixes
introduced — materialization, two keys, the 3-way diff, constant-work resolver,
checkpoints, snapshot undo.

---

## Part A — Verification of R1 perf fixes

| R1 finding | v1 claim | Verdict |
|---|---|---|
| **S1-1** current-value derived at read | `fact_current` materialized, txn-maintained; `fa_subject_live`, `fa_asof` indexes; `valid_from_sortkey` precomputed (§3.1) | **CONFIRMED-FIXED for reads** — the read path is now an indexed point/range lookup. *But* the maintenance cost moved onto the write path uncosted → **R2-N1**. |
| **S2-1** Stage-2 per-candidate + whole-corpus re-extract | per-note cached retrieval; shared-context batching; incremental re-extract scope from the diff (§5.1/§5.3) | **CONFIRMED-FIXED in shape.** Caveat: "incremental scope from the diff" is asserted; the blast-radius computation itself is uncosted and the 3-way diff it feeds is new cost → **R2-N3**. Extraction LLM cost per *fresh* note is unchanged (still 1+k); that was accepted, not a regression. |
| **S3-1** projections multiply + un-indexable resolver | attribute-free global resolution index; `identity_merge` = redirect not assertion-rewrite (§3.5) | **PARTIALLY FIXED.** The global index restores an indexed *recall* target — real win. But the **resolver is now mandated constant-work** (§6.3b), which converts a fast-path-on-hit lookup into a forced worst-case scan on the ingest hot path → **R2-N4 (the headline question).** |
| **S2-2** append-only growth / as-of scans | `fa_asof` `(owner,live_key,tx_from DESC)`; no-op suppression; live/archived partition *option* (§3.1/§5.3) | **CONFIRMED-FIXED** for as-of and growth-rate. Gap: partitioning is still only an "option", and `fa_asof` leads with `live_key` (32-byte bytea) — wide key, see **R2-N2**. History bloat itself is bounded by no-op suppression, good. |
| **S2-3** O(members) op rows + lock contention | capped/paginated fat-read; lazy per-cell enrichment; bounded sub-batches w/ parent `batch_id`; canonical slot-key-ordered advisory locks (§4.3) | **CONFIRMED-FIXED.** Bounded batch + ordered locks is the right answer. Residual surfaces only in snapshot-undo of a bounded batch → **R2-N6** (SEV-3, bounded). |
| **S2-4** rrule next-occurrence unindexed/unbounded | `next_occurrence_at` cached column + `fa_next_occ` index; expand from `max(dtstart,now)`; uncapped high-freq rule rejected (§2.6) | **CONFIRMED-FIXED.** Cache + index + `after(dt)` + committer cap is exactly R1's mitigation. One stale-cache refresh path (read-past-`next_occurrence_at`) is bounded. |
| **S2-5** per-fact backstop CPU + vector query on reprocess | cache canonicalization by `raw_predicate`; reuse Stage-2 retrieval for D1; content-hash short-circuit on reprocess (§5.2/§5.3) | **CONFIRMED-FIXED.** Content-hash short-circuit is the dominant lever and it is present. |

**STILL-OPEN from R1:** none of the seven is left unaddressed; **S1-1, S2-2, S3-1
have residual cost that v1 *relocated* rather than removed** — those residuals are the
NEW findings below, not reopened R1 findings. The fixes are real; they were not free.

---

## Part B — NEW costs introduced by the R1 fixes

### R2-N1 · `fact_current` transactional maintenance amplifies every write and serializes hot-subject ingest
**Severity: SEV-2**

**Hot path.** Every committer op (ingest commit, supersession, retime, pin, confidence,
human edit, reprocess write) now does, **in the same transaction** (§1.5, §3.1, §3.4
"one transaction per op"): (1) insert into `fact_assertion`, (2) maintain
`one_live_per_live_key` partial unique index, (3) **upsert/delete the corresponding
`fact_current` row**, (4) write the `fact_op` row. The R1 fix (S1-1) made `fact_current`
*authoritative-cache, maintained in the op txn* — so the read win is paid for on every
write.

**Cost reasoning.**
- **Write amplification factor ≈ 2× index+row work per op** vs an append-only-only
  store: the assertion insert *plus* a `fact_current` upsert (itself maintaining its own
  PK + any `(subject,domain)` read index). For a functional supersession it is a
  `fact_current` row *replace* (delete old live + insert new); for a set add it is an
  insert. This is O(1) per op — **not** a scaling cliff by itself.
- **The real cost is lock/serialization scope, not row count.** The partial unique
  index `one_live_per_live_key WHERE ... modality='asserted'` plus the `fact_current`
  upsert means **two concurrent ops touching the same `live_key` serialize** on the
  unique-index page + the `fact_current` PK row. Per R1's own note, "ingest + an open
  review session on the same entity" is the realistic concurrent pair. A long
  multi-statement review transaction (§4.3) that touches a hot subject's slots now holds
  the `fact_current` rows for those slots for the whole transaction, blocking concurrent
  reprocess/ingest of the same subject.
- **Reprocess interaction:** a major re-analysis (§5.3) replays ops across the corpus;
  each non-suppressed write also maintains `fact_current`. No-op suppression (S2-5) keeps
  this bounded, but any *changed* fact pays the full `fact_current` maintenance — so a
  contract bump that touches a common field rewrites `fact_current` for O(affected
  slots), single-threaded if it must preserve op ordering per subject.

**Where it falls over.** Not on row count (O(1)/op) but on **write latency under the
long review transaction** and on **reprocess throughput** if `fact_current` maintenance
forces per-subject serialization. The spec says "maintained in the same op transaction"
but never bounds the transaction's lock footprint on `fact_current` or specifies the
isolation level — at `SERIALIZABLE` the unique-index + `fact_current` contention can
produce serialization failures + retries under concurrent ingest/review.

**Mitigation.**
1. **Specify `fact_current` keyed exactly on `live_key`** (1:1 with the live partial
   index) so the upsert is a single-row point write, never a range; index it
   `(owner_id, subject_id, domain_code)` for entity-card reads. (The spec implies this
   but does not state the PK.)
2. **Bound the review transaction's `fact_current` footprint:** apply review ops as
   bounded sub-batches (already mandated for assertions, §4.3) and **extend the same
   sub-batch bound to `fact_current` upserts**, releasing locks between sub-batches, so a
   wide review never holds a hot subject's current rows for a multi-second human-paced
   transaction. (Human think-time must never be inside the writing transaction — stage
   ops, then commit.)
3. **State the isolation level + retry policy** for committer txns; prefer
   `READ COMMITTED` + the partial-unique-index as the correctness backstop over
   `SERIALIZABLE`, so concurrent ops on *different* `live_key`s never serialize.
4. **Reprocess writes `fact_current` via a rebuild-into-shadow + swap** for whole-corpus
   passes, not row-by-row in the live table, so the live read path isn't degraded
   mid-migration and per-subject ordering is a batch property, not a lock.

---

### R2-N2 · The two-key scheme doubles per-row key maintenance and makes predicate re-canonicalization an O(history) re-key
**Severity: SEV-2**

**Hot path.** Every assertion now carries **two hashed keys** — `identity_key`
(value-including) and `live_key` (value-excluding for functional) — plus `value_identity`
(§3.2). Both are `bytea` hashes over `(owner, subject, predicate, qualifier, domain,
modality[, value])`. Every write computes both; the live partial unique index is on
`live_key`; `fa_asof` leads with `live_key`. **Predicate `canonical` is an input to BOTH
keys.**

**Cost reasoning.**
- **Per-write:** two hash computations + two indexed columns maintained. O(1) per op,
  cheap in absolute terms (a couple of µs of hashing). Not the problem.
- **The problem is key STABILITY under registry evolution.** The spec hard-couples the
  key to `predicate.canonical`, `domain`, and `modality`. Three v1 mechanisms *change*
  those inputs after rows exist:
  - **Predicate re-canonicalization** (§5.2 C2, the embedding-assisted registry can merge
    a drift spelling into a canonical, or the threshold tunes — §7 still-open (ii)). When
    `worksFor`→`person.employer` re-canonicalizes, **every historical assertion whose key
    hashed the old canonical is now mis-keyed**: its `live_key`/`identity_key` no longer
    group with the canonical's other rows. Re-keying is **O(assertions for that
    predicate)** — a full re-hash + index rewrite, exactly the "silently-stale hash" R1
    S3-1 warned about, now applying to the predicate axis the two-key scheme made
    load-bearing.
  - **`merge_entities`** already triggers a slot re-key / member-dedup pass (§3.2) —
    O(live members for the two entities). Acknowledged, bounded.
  - **`move_domain`** changes `domain` → changes both keys for the moved row; bounded to
    the moved value (one-way, single row).
- **`fa_asof` and the unique index lead with a 32-byte bytea key.** B-tree on a 32-byte
  random hash has poor key density (fewer entries per page than an int/uuid composite) and
  no range locality, so as-of scans `(live_key, tx_from DESC)` get a wider index and the
  hash gives no clustering — every distinct slot is a random page. At 10^6 rows this
  inflates the index size and cache-miss rate versus a `(subject_id int, predicate_id
  smallint, tx_from)` composite.

**Where it falls over.** Not steady-state ingest (the double-hash is trivial) but
**registry evolution**: a single predicate-canonicalization merge — a routine,
expected operation in an embedding-assisted registry — triggers an O(history-for-
predicate) re-key + index rewrite, and there is **no re-key op, cost bound, or
incremental plan specified** for it. The two-key scheme bought clean history grouping
(R1 S1-4) at the cost of making the most common registry operation a bulk rewrite.

**Mitigation.**
1. **Do not hash the mutable canonical into the key.** Key on a **stable surrogate
   `predicate_id`** (registry-assigned, immutable; canonicalization changes the *display*
   mapping, not the id) so re-canonicalization is a registry-row update, not an
   assertion re-key. Same for domain: hash a stable `domain_id`, not `domain_code`.
2. If the canonical *must* be in the key, **specify the re-canonicalization op as a
   bounded, audited, incremental re-key** (batched like reprocess, no-op-suppressed,
   parent `batch_id` for undo) — and cost it explicitly; do not leave it implicit.
3. **Lead `fa_asof` and the unique index with `(subject_id, predicate_id)` integer
   columns**, with the hash key as a tiebreak/covering column — recovering B-tree key
   density and clustering on the single-owner partition.

---

### R2-N3 · The 3-way human-op-overlay diff is O(corpus facts × overlay ops) on every re-analysis, with no incremental bound
**Severity: SEV-2**

**Hot path.** Re-analysis (§5.3) is now a **3-way diff**: `(old machine facts ⊕ human-op
overlay)` vs `new machine facts`. Per re-emitted claim the committer must check, against
the *overlay*: (a) does an `identity_key`(+`value_identity`) match a **human
retraction/removal post-dating the most recent supporting note edit** (M2)? (b) is any
field **human_touched since the last note edit** (M3)? (c) does the provenance span
**overlap a span covered by a human `split_fact`/`merge_facts`/`add_fact`** (S2-1)?

**Cost reasoning.**
- **Overlay construction:** building "(old machine facts ⊕ human-op overlay)" requires
  replaying or indexing the human-op subset of `fact_op` over the affected slots. If done
  naively per re-extracted claim it is O(re-extracted claims × overlay-ops-touching-that-
  slot). With the right index it is O(1)/claim, but **the index is not specified.**
- **Check (c) is the dangerous one: span-overlap.** "Does this claim's provenance span
  overlap a span a human split/merge/add covered" is a **range-overlap query over the
  human-op span set per note**. Without a per-note span index on human structural ops
  this is O(human-structural-ops-in-note) per re-extracted claim → O(claims × ops) per
  note. A note heavily corrected by a human (many splits/adds) makes its own
  re-extraction superlinear.
- **Multiplier:** runs over **every re-extracted note** in the re-analysis scope. The
  incremental-scope mitigation (S2-1/§5.3 "blast radius from the diff") bounds *which*
  notes, but within each note the 3-way diff is paid per claim. The blast-radius
  computation itself ("which facts touch the changed contract field") is **uncosted** —
  it implies a scan/index over all facts by contract-field usage that the schema does not
  provide.

**Where it falls over.** A **major contract bump on a commonly-used field**: blast radius
≈ whole corpus, and each note pays the 3-way diff including the span-overlap range check
against its human-op history. For the most-corrected notes (precisely the high-value
ones) this is superlinear. The spec treats the 3-way diff as a correctness mechanism and
never gives it a query plan or index.

**Mitigation.**
1. **Materialize the overlay as a queryable structure**, not a replay: a
   `human_overlay(slot_key/live_key, field, op_kind, span_range, effective_after_note_edit)`
   index maintained by the committer whenever a human op lands, so each of (a)/(b)/(c) is a
   point/range lookup, O(log n)/claim.
2. **Index human structural-op spans with a GiST range** per note (`note_id, span_range`)
   so check (c) is an indexed overlap, not a per-claim scan.
3. **Specify the blast-radius index:** a `fact ↔ contract-field-used` mapping (or derive
   it from `value_shape`/predicate set) so "which notes touch the changed field" is a
   lookup, not a full scan — otherwise every "incremental" migration starts with an
   O(corpus) scan that defeats the incrementality.

---

### R2-N4 · Constant-work resolver pays worst-case entity-resolution on EVERY ingest claim — a real ingest-latency tax
**Severity: SEV-2** (answering the framing's direct question: **yes, it is real, but
it is a throughput tax, not a scaling cliff** — bounded by the global index size, which
is the thing R1 made indexable.)

**Is "constant-work resolver" a real latency problem at ingest?** **Yes — by
construction it removes every fast-path early-exit, so it is the *maximum*, not the
*expected*, cost on the hot path; but it is bounded and ingest is async, so it is SEV-2,
not SEV-1.** Reasoning:

**Hot path.** §6.3b mandates the cross-domain resolver is **constant-work / constant-time
w.r.t. the protected side: always runs the full candidate set, decoy-padded, no early
exit on a protected match** (the R1-S1 timing-oracle fix). The resolver runs over the
attribute-free global index (§3.5) for **every cross-domain mention** at extraction
(§3.5: "the cross-domain resolver operates over this index ... on the ingest hot path").

**Cost reasoning.**
- "Constant-work" means: even when the obvious match is the first candidate, the resolver
  **must still process the full decoy-padded candidate set** — it deliberately forfeits
  the average-case win that ANN/early-exit normally gives. So per cross-domain mention the
  cost is **fixed at the worst case = O(candidate-set-size + decoy-pad)**, *every time*,
  not amortized.
- **Magnitude:** with a well-built attribute-free HNSW index the candidate set is a small
  top-K (say 20–50) plus decoy padding to a fixed width. That is **bounded and small in
  absolute terms** (sub-millisecond to low-ms of vector + hash compares), and crucially it
  is **bounded independent of corpus size** because the global index returns a fixed-width
  candidate set. So this is NOT O(global-entity-set) per mention — the R1 S3-1 "O(global
  entity set), un-indexable" failure is genuinely fixed by the indexable global skeleton.
- **The tax is the constant factor × frequency.** It is paid per cross-domain mention,
  rate-limited and audited in both domains (§6.3d) — and **the audit write + rate-limit
  check is itself per-invocation overhead** on the ingest path. For a note that mentions
  several cross-domain entities (a health note naming family members who also exist in
  general), that is several forced-worst-case resolutions + several dual-domain audit
  writes per note.

**Where it lands.** Ingest is an **async, batched** pipeline (Stage-1 → Stage-2 →
validate → commit), not a synchronous user-blocking request. A fixed small constant per
cross-domain mention, even forced-worst-case, is absorbable in an async pipeline whose
dominant cost is already the LLM round-trips (S2-1, orders of magnitude larger than a
vector compare). **The resolver's constant-work is NOT the ingest bottleneck — the LLM
calls are.** It becomes a problem only if (i) the decoy-pad width is set large "to be
safe", or (ii) the dual-domain audit write is synchronous + unbatched, or (iii) it is
invoked per *candidate* rather than per *mention* (the S2-1 `3k` trap reappearing).

**Mitigation.**
1. **Bound the candidate-set + decoy-pad width explicitly** and make it a fixed constant
   independent of corpus (a config'd K), so "constant-work" is a *small* constant, not a
   conservatively-huge one. Document the chosen width and its timing-oracle margin.
2. **Resolve once per mention, not per candidate fact** (reuse the per-note cached
   retrieval of S2-1) — a note's entity resolution is a per-mention-set operation; never
   re-run the constant-work resolver for each of a mention's k facts.
3. **Batch the dual-domain audit writes** for a note's resolutions into the commit
   transaction, not one synchronous write per resolution.
4. **Keep the constant-work guarantee scoped to the protected→general timing channel
   only.** General↔general resolution (no firewall crossing) needs no decoy padding and
   can keep its normal ANN early-exit — do not pay the worst-case tax where there is no
   oracle to defeat.

---

### R2-N5 · Immutable checkpoint storage grows with the materialized graph × checkpoint frequency, with no retention policy specified
**Severity: SEV-3**

**Hot path.** §1.2/§3.4/§4.5: forensic replay + archived-log undo bound to the nearest
**immutable checkpoint of the materialized graph**; `op_checkpoint(graph_snapshot_ref)`.
The op-log "can be archived behind a checkpoint, never truncated."

**Cost reasoning.**
- A checkpoint of the *materialized graph* is, at minimum, a snapshot of `fact_current`
  (≈ live-set size, 10^4–10^5 rows). **Each checkpoint ≈ O(live set).** Checkpoint storage
  = O(live set × number-of-checkpoints).
- The spec gives **no checkpoint cadence and no checkpoint-retention/compaction policy.**
  Too-frequent checkpoints → storage grows fast (full live-set snapshots); too-sparse →
  forensic replay must replay many ops from the last checkpoint (the cost checkpoints
  exist to bound). Either way an unspecified knob.
- The op-log "never truncated, only archived behind a checkpoint" means **`fact_op` grows
  without bound** for the system's lifetime — fine for cold storage, but the archive
  boundary, the storage tier, and the index-on-archived-rows policy are unspecified.

**Where it falls over.** Not soon — at single-owner scale this is gigabytes over a
decade, not a cliff. It falls over only as **operational debt**: unbounded `fact_op`
growth + full-live-set checkpoints with no cadence/retention is a "works for years, then
the snapshot job and the op table are quietly huge" problem.

**Mitigation.**
1. **Specify checkpoint cadence as op-count- or size-triggered** (e.g. checkpoint every N
   ops or when log-since-checkpoint exceeds replay-budget), not time-based.
2. **Make checkpoints incremental/differential** where possible (delta from prior
   checkpoint), since most of `fact_current` is unchanged between checkpoints — full
   snapshots are wasteful.
3. **State an archive tier + retention** for cold `fact_op` rows behind a checkpoint
   (move to compressed/columnar cold storage; keep only checkpoint + post-checkpoint hot
   ops in the live table) so the live op table stays bounded.

---

### R2-N6 · Snapshot-revert undo of a large bounded batch is O(batch members) re-validation + dependency-graph walk
**Severity: SEV-3** (bounded — the batch cap from S2-3 is the saving grace)

**Hot path.** §1.2/§4.5: undo is **snapshot/state-based** — tombstone the op's assertions,
un-tombstone what it superseded, **gated by the undo-dependency check** (no later live op
depends on this op's outputs). Batch undo operates at batch granularity through the same
dependency graph (M11).

**Cost reasoning.**
- Undo of a single op: tombstone its assertion(s) + un-tombstone superseded + a dependency
  check (scan for later ops whose target `live_key`/`value_identity`/entity intersects this
  op's outputs). The dependency check is the cost — **O(later ops touching the same
  slots)**, which for a hot subject's much-edited slot can be many.
- **Batch undo** of a bounded sub-batch touching M members = M× the above + a transitive
  dependency walk if any member has live dependents (cascade preview). Worst case the walk
  fans out across the dependency DAG. The **batch cap from S2-3 bounds M**, so this can't be
  unboundedly large — that is exactly why this is SEV-3 not SEV-2.
- Each undo also re-maintains `fact_current` for the un-tombstoned/tombstoned rows
  (R2-N1 cost, M× per batch) and, for `add_fact` undo, retracts the auto-drafted
  correction note (M9) — a couple extra writes per member.

**Where it falls over.** Only the **dependency-graph walk for a deeply-composed,
much-superseded slot** — undoing an old op under a tall supersession stack with many later
dependents triggers a cascade preview that walks the DAG. Bounded by batch size + dependency
fan-out; defined, not magic (the spec says as much in §8 residual). Not a scaling cliff.

**Mitigation.**
1. **Index the dependency edges**: maintain `op_dependency(op_id → depends_on_op_id)` (or
   derive from `supersedes` + `target_live_key`) so the dependency check is an indexed
   reverse lookup, not a scan of later ops.
2. **Bound + paginate the cascade preview** the same way the fat-read is bounded (S2-3) —
   a cascade touching > N ops surfaces a count + paged preview, never an unbounded build.
3. **Reuse the sub-batch parent `batch_id`** (already in `fact_op`) as the unit of undo so
   batch undo is a `WHERE batch_id=? OR parent_batch_id=?` set operation, not M
   independent op-undos.

---

## Cross-cutting recommendation (R2)

The R1 fixes were correct in shape; **three of them relocated cost from the read path to
the write path or to registry-evolution**, and the spec costed the read win but not the
relocated cost:

1. **`fact_current` (R1-S1-1)** moved cost onto every write + the long review txn →
   bound the txn's `fact_current` footprint, pin the isolation level (R2-N1).
2. **Two keys (R1-S1-4)** made `predicate.canonical` load-bearing in a hash → key on a
   **stable surrogate `predicate_id`**, not the mutable canonical, so re-canonicalization
   isn't an O(history) re-key (R2-N2). *This is the single highest-leverage fix in R2.*
3. **3-way overlay diff (R1-M2/M3/S2-1)** needs a **materialized, indexed overlay** + a
   **span GiST** + a specified **blast-radius index**, or "incremental" re-analysis still
   begins with an O(corpus) scan (R2-N3).

On the framing's direct question — **is the constant-work resolver a real ingest-latency
problem? No, not as a scaling cliff** (R2-N4): it is a fixed *small* constant per
cross-domain *mention*, bounded independent of corpus by the attribute-free global index,
and dwarfed by the LLM round-trips that already dominate ingest. It is a tax only if the
decoy-pad is over-wide, it is run per-candidate instead of per-mention, or the dual-domain
audit write is synchronous/unbatched — all three are specifiable away.

---

## Severity summary

| ID | Title | Sev |
|---|---|---|
| R2-N1 | `fact_current` txn maintenance amplifies writes + serializes hot-subject ingest | **SEV-2** |
| R2-N2 | Two-key scheme: predicate re-canonicalization is an O(history) re-key (canonical hashed into key) | **SEV-2** |
| R2-N3 | 3-way human-op-overlay diff has no incremental bound / no overlay+span+blast-radius index | **SEV-2** |
| R2-N4 | Constant-work resolver ingest tax — real but bounded; not a cliff | **SEV-2** |
| R2-N5 | Checkpoint storage + unbounded op-log growth, no cadence/retention policy | **SEV-3** |
| R2-N6 | Snapshot-undo of a bounded batch: O(members) re-validation + dependency walk | **SEV-3** |

**No SEV-1.** All seven R1 perf findings are confirmed fixed in shape; their residual is
relocated cost, captured above as SEV-2/3 with specific index/materialization fixes. The
highest-leverage single change is **R2-N2** (key on a stable `predicate_id`, not the
mutable canonical) — without it, routine registry canonicalization is a bulk history
rewrite.
