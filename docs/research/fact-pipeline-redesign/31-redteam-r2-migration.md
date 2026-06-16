# Red-team R2 — Migration, back-compat & reversibility (Round 2)

**Lens:** MIGRATION, BACK-COMPAT & REVERSIBILITY. Adversarial.
**Target:** `21-spec-v1.md` (revised after R1; +`00-framing.md` §4 invariants, `01-decisions-log.md` D1).
**Scope note:** R1's M7 (existing-corpus in-place migration) is OUT OF SCOPE per D1 (clean
rebuild) and is NOT re-litigated here. FUTURE contract-version re-analysis (D1 "still in
scope") IS attacked.
**Method:** (1) verify R1 Sev-1/Sev-2 fixes hold; (2) attack v1's *new* reversibility
posture — snapshot undo with cascade-or-block — against the framing §4 invariant "every
committed change is … unwindable"; stress the 3-way human-op overlay under a future
re-analysis; test checkpoint/replay forensic soundness. Grade Sev-1 (breaks an invariant or
a core goal) … Sev-3 (nit).

---

## Part 1 — R1 verification (did the fixes land?)

| R1 finding | v1 mechanism | Verdict |
|---|---|---|
| **M1** non-LIFO undo undoes wrong state | Snapshot/state-based undo (tombstone op's assertions, un-tombstone superseded) + undo-dependency graph; cascade-or-block; "remove k keep k+1" is a forward correction (§1.2, §4.5) | **CONFIRMED-FIXED as a row-level mechanism** — but the *invariant promise* is silently weakened (see R2-1). |
| **M2** re-analysis resurrects retracted | 3-way diff `(old machine ⊕ human overlay) vs new machine`; retraction/removal post-dating note edit → review (§5.3) | **CONFIRMED-FIXED for the simple case** — overlay edge-cases survive (see R2-3). |
| **M3** pinned/human-edited overwritten | Protect `human_touched` not just `pinned`; per-pin shape-lift-or-blocker SLA (§3.4, §5.3) | **CONFIRMED-FIXED in mechanism** — `human_touched` reset semantics are underspecified (see R2-4). |
| **M4** downgrade laundering | One-way downgrade; undo purges the general value (tombstone); move-lineage non-resurrectable (§6.4, §7f) | **CONFIRMED-FIXED.** This is the cleanest R1 fix; "one-way" closes the flow-reversibility gap honestly. |
| **M5/M6** unmerge/split inverses unsafe | Snapshot+dependency-gated undo; unmerge-after-writes = reviewed split; diverged children → cascade preview (§4.1) | **CONFIRMED-FIXED in mechanism** — but see R2-2 (snapshot reconstructs wrong state) for the residual. |
| **M8** genesis-replay | Dropped as a guarantee; ops freeze resolved outputs + pipeline-tuple; replay to nearest immutable checkpoint (§1.2, §3.4) | **CONFIRMED-FIXED for graph reconstruction** — checkpoint *integrity/forensic* soundness is newly attackable (see R2-5). |

**Net:** every R1 Sev-1/Sev-2 has a real, named mechanism in v1; none are STILL-OPEN as
originally framed. The remaining attack is on the *new posture those fixes introduced* — the
spec traded "false total-inverse" for "honest conditional undo," and the conditional has its
own holes.

---

## SEV-2 findings

### R2-1 (SEV-2) — "cascade-or-block" does not satisfy framing §4 "every change is unwindable"; it redefines the invariant rather than meeting it

**Claim attacked:** §8 invariant check ticks "Audit & reversibility — ✔ … snapshot-based undo
with an explicit dependency graph (cascade or block — no false 'total inverse' claim)." §1.2:
"Selective mid-history 'remove k's delta but keep k+1' is a **new forward correction**, not an
undo."

**Why it breaks:** the framing §4 invariant is binary and unconditional: "every committed
change is traceable and **unwindable** (reopen/undo)." v1 satisfies a *different* proposition:
"every committed change is either unwindable OR its unwind is blocked pending the unwind of its
dependents." Those are not the same invariant. Three concrete user-expectation gaps:

1. **Block is not unwind.** If op *k* has a live dependent *k+1* that the user does **not** want
   to lose, the user's only sanctioned paths are (a) cascade — also lose *k+1* (collateral), or
   (b) "issue a new forward correction" — which is *not* undoing *k*; it leaves *k* in the audit
   trail as applied, and constructs a hand-built compensating state the user must get right
   themselves. The thing the invariant promised — "I can take back change *k*" — is **not
   available** for any *k* with a live dependent. The spec relabels this as "out of scope of
   undo" rather than admitting the invariant is now conditional.

2. **Cascade is lossy undo, sold as undo.** A cascade undo of *k* that drags *k+1*…*k+n* with it
   silently discards later, *independently-correct* human work. Example: op *k* = `set_field
   value` on F (machine); op *k+1* = human `retime` on F (correct, unrelated to the value).
   Undoing *k* (the machine value) cascades the human `retime` because the dependency graph keys
   on the same `slot_key`/`live_key`. The human's good `retime` is collateral. The user expected
   "fix the value"; they got "lose my temporal correction too."

3. **The §8 checkmark is doing rhetorical work.** "no false 'total inverse' claim" is presented
   as a *strengthening*, but framing §4 demands total unwindability, not honesty about its
   absence. An honest *partial* is still a *partial*. The spec should either (a) carry this to
   the user as an **explicit, bounded doctrine change** to invariant §4 ("unwindable" →
   "unwindable, possibly via audited cascade, else blocked with a named-dependent error"), the
   way #7 was explicitly surfaced — or (b) be marked ACCEPTED-RISK in §9. It is currently neither;
   it is ticked ✔ as if §4 were met as written.

**Concrete interleaving (block surprise):**
1. Machine `set_field value` F: `5.4→5.8` (op1).
2. Human `pin` F (op2) — `set_lifecycle{pin}`, depends on F's current live row (op1's output).
3. User realizes the *5.8 extraction was a misread* and tries to undo op1.
4. Dependency check: op2 (`pin`) is a live dependent of op1's output row → **blocked**. To undo
   the misread value, the user must first undo their own deliberate `pin`. The UX presents
   "unpin to proceed" — but the user wanted the pin; the pin was correct, the *value under it*
   was wrong. The invariant said op1 is unwindable; in practice it is hostage to op2.

**Severity rationale:** Sev-2, not Sev-1 — the mechanism is *sound and auditable*, no data is
corrupted, and a determined user can reach the desired state via forward correction. But it
**does not satisfy framing §4 as written**, and the §8 self-assessment claims it does. A binding
invariant being silently re-scoped is exactly what the migration lens exists to catch.

**Fix:** Pick one and state it:
- **(a) Doctrine amend (recommended):** explicitly rewrite invariant §4 to "every committed
  change is unwindable via undo *or* audited cascade-undo; where a live dependent blocks, the
  block names the dependent and offers cascade-or-forward — there is no silent un-unwindable
  state." Surface to the user as a bounded doctrine change, parallel to the #7 reconciliation.
- **(b) Per-field dependency granularity:** key the dependency graph on `(slot_key, field)` not
  `slot_key`, so a `retime` (temporal field) is **not** a dependent of a `set_field value` (value
  field) on the same slot. This rescues interleaving #2 (the collateral-`retime` case) and shrinks
  the cascade blast radius to genuinely value-dependent ops. R1's M1 fix says "same `slot_key`/
  `value_identity`/entity"; tightening to field-granularity is strictly safer and removes most
  surprise.
- Pinning/lifecycle ops should be modeled as **non-blocking annotations** that *travel with* the
  row they pin (re-attach to the un-tombstoned predecessor on undo) rather than as dependents that
  block the undo of the value beneath them.

---

### R2-2 (SEV-2) — snapshot-revert reconstructs a WRONG state when undo crosses a supersession chain whose middle link was itself a human edit

**Claim attacked:** §1.2/§4.5 — "Undo = tombstone the assertions a target op wrote and
un-tombstone the ones it superseded (read off `op_id` + `supersedes`)." Presented as a clean,
deterministic state restoration.

**Why it breaks:** un-tombstoning "the ones it superseded" restores the *immediately prior*
assertion row. But in a supersession **chain** A←B←C (C supersedes B supersedes A), where the
links have different actors and the middle one is a human correction, undoing the **machine** op
that wrote C does not restore the state the user remembers — it restores B, which may be a row
the human had already *meant to be gone*, or restores B with a stale `human_touched`/`pinned`
flag that no longer reflects reality.

**Concrete interleaving (wrong-state reconstruction):**
1. Machine writes A (`title=Engineer`, op_a).
2. Human `replace_member` → B (`title=Senior Engineer`, op_b, `human_touched=true`). B supersedes A.
3. Re-analysis (`actor=reprocess`) writes C (`title=Staff Engineer`, op_c) — *should* have been
   blocked by the M3 `human_touched` guard, **but** the guard fires only "since the last note
   edit"; the note was edited between op_b and op_c (the user added "promoted to Staff"), so
   `human_touched` was **reset** (see R2-4) and C auto-commits, superseding B.
4. User undoes op_c (the reprocess). Snapshot-undo un-tombstones **B** (op_c's `supersedes`
   target). State is now B = "Senior Engineer." But the *note now says Staff*. The graph
   disagrees with the source of truth, and the un-tombstoned B carries `human_touched=true` —
   so a subsequent reprocess is now **wrongly blocked** from correcting it, because undo
   resurrected a stale human-touched flag on a row whose human intent was already superseded.

The deeper bug: **`supersedes` records row lineage, not *semantic* lineage across actor
changes.** Un-tombstoning the predecessor is correct only when the predecessor's *metadata*
(flags, certainty, human_touched) is still valid in the post-undo world. After an actor change
mid-chain, it is not. The snapshot is a row snapshot; the *protection metadata* is not part of
what undo reasons about.

**Severity rationale:** Sev-2 — produces a graph state that (a) contradicts the note (source of
truth) and (b) carries a resurrected stale protection flag that mis-gates future re-analysis.
Contained to multi-actor supersession chains, detectable via audit, not a firewall break — but
it is a *wrong-state* reconstruction, exactly the R1 M1/M5/M6 failure class re-emerging one level
up (at the metadata layer rather than the value layer).

**Fix:**
- Undo must un-tombstone the predecessor row **and re-derive its protection metadata** from the
  op-log as-of the undo point, not trust the frozen flag on the row. Specifically `human_touched`
  must be **recomputed** ("is there a live `human:*` op on this slot since the last note edit?")
  on un-tombstone, never resurrected verbatim.
- Where the un-tombstoned predecessor would contradict the current note content (its supporting
  span no longer matches), undo must **route to review** ("undo would restore a value the note no
  longer supports"), not silently commit a note-contradicting row.

---

## SEV-3 findings

### R2-3 (SEV-3) — 3-way overlay double-applies a human edit that the future contract *also* now produces natively

**Claim attacked:** §5.3 — "Human-touched … frozen against re-extraction; conflicting re-extracted
values route to review." The overlay assumes human edits and machine re-extractions are *distinct
deltas* to reconcile.

**Why it breaks (double-apply):** a human op and a future contract can encode the **same
correction by different means**. Example: under v(n) the extractor could not emit a `bound=unknown`
"former without date," so the human issued `retime{mark_former}` (op in the overlay). v(n+1)'s
extractor *can* now natively emit `bound=unknown` from "used to work at Acme." On re-analysis:
the machine-new fact already has `bound=unknown`; the human overlay *also* says `bound=unknown`.
The 3-way diff sees machine-new == human-overlay value on that field → no conflict → but the
question is which **provenance/certainty** wins. If the overlay is applied *over* the now-correct
machine fact, the field is stamped `human_correction` + `certainty=inferred` when it should be
`extracted` + `asserted` (the note now supports it natively). The fact is *correct in value* but
*wrong in provenance/certainty* — and stays wrongly `human_touched`, permanently frozen against
all future re-analysis for a correction the machine no longer needs.

**Severity rationale:** Sev-3 — value is correct; only provenance/certainty/freeze-status drift.
But it accretes: every "the contract caught up to the human" case leaves a permanently-frozen slot,
slowly ossifying the graph against improvement.

**Fix:** the 3-way diff must detect **overlay-subsumed-by-machine**: when machine-new independently
reproduces the human edit's *value*, the human op is marked `superseded-by-contract` (audited), the
field reverts to machine provenance, and `human_touched` is **cleared** for that field — the human
correction is honored by being made redundant, not by freezing the slot forever.

### R2-4 (SEV-3) — `human_touched` "since the last note edit" reset is the load-bearing predicate for M2/M3 and is underspecified

**Claim attacked:** §3.1 column `human_touched boolean … (any human op since last note edit)`;
§5.3 "frozen … since the last note edit." This "since last note edit" clause silently gates *both*
the M2 resurrection guard and the M3 protection.

**Why it breaks:** "last note edit" is ambiguous and powerful. (a) A *trivial* note edit (fixing a
typo elsewhere in the note, adding an unrelated sentence) resets `human_touched` for **every** fact
sourced from that note — re-opening all of them to auto-overwrite by re-extraction, silently
dropping the M3 protection the user relied on. (b) Conversely, if "note edit" means "any change to
the supporting *span*," then editing a *different* span in the same note does not reset — but the
spec says "note," not "span." The two readings have opposite safety properties, and the spec picks
neither. R2-2 step 3 is a direct exploit of reading (a).

**Severity rationale:** Sev-3 only because it is a specification gap, not yet a demonstrated
corruption — but it is the hinge of two Sev-1-origin fixes, so it must be nailed down.

**Fix:** define the reset at **span granularity, not note granularity**: `human_touched` on a
field is cleared only when the *specific supporting span* the human edit was grounded against
materially changes (or is deleted). An edit elsewhere in the note never resets protection for a
fact whose span is untouched. State this precisely in §5.3 and add an eval fixture (edit-elsewhere-
in-note must NOT drop protection).

### R2-5 (SEV-3) — checkpoint forensic soundness assumes checkpoints are trustworthy, but nothing binds a checkpoint to the op-log it claims to summarize

**Claim attacked:** §3.4 `op_checkpoint(as_of_op, graph_snapshot_ref)`; §4.5 "Forensic
reconstruction replays *recorded outcomes* to the nearest immutable checkpoint." Replay soundness
now *rests* on the checkpoint being a faithful materialization of the graph as-of `as_of_op`.

**Why it breaks:** the checkpoint is `graph_snapshot_ref text` — an opaque external blob pointer.
Nothing in the schema (a) hashes the op-log prefix `[genesis..as_of_op]` into the checkpoint, or
(b) lets a forensic verifier *prove* the checkpoint equals "replay of recorded outcomes up to
`as_of_op`." If a checkpoint is wrong (a bug in the materializer, a partial write, an after-the-fact
edit to the blob store), forensic replay from that checkpoint forward produces a *confidently wrong*
history with no detection — and the whole point of dropping genesis-replay (M8) was that
checkpoints are the trust anchor. An untrusted trust anchor is worse than genesis-replay, because
genesis-replay was at least *self-verifying from the log*.

**Severity rationale:** Sev-3 — checkpoints are infrastructure the spec controls, the failure
requires a materializer bug or blob tampering, and it is forensic-only (not a live-path break). But
the audit invariant now depends on checkpoint integrity that is unspecified.

**Fix:** add to `op_checkpoint` a `prefix_hash bytea` = hash of the ordered `(op_id,
resolved_outputs)` chain `[genesis..as_of_op]`, and a `graph_hash bytea` of the materialized
snapshot. A checkpoint is *valid* iff re-materializing recorded outcomes over the previous
checkpoint reproduces `graph_hash`, chained back to genesis. Forensic replay must verify the
prefix-hash chain before trusting any checkpoint; a hash break is a hard audit alarm, not a silent
wrong answer.

---

## Verdict on the weakened undo promise

The trade v1 made — *false total-inverse* (R1's lie) → *honest snapshot undo with cascade-or-block*
— is the **right direction** and the mechanism is sound, auditable, and non-corrupting at the row
level. But it does **not** satisfy framing §4's literal "every committed change is unwindable": it
satisfies "unwindable-or-blocked-or-rebuild-by-hand." That is a **real, if modest, weakening of a
binding invariant**, and §8 currently ticks it ✔ as though §4 were met as written. The weakening is
**acceptable in engineering terms** (no sound alternative exists short of full inverse-rebase, which
R1 correctly killed) — but it is **NOT acceptable to ship it unacknowledged**. It must be carried to
the user as an explicit, bounded amendment to invariant §4 (parallel to the #7 doctrine
reconciliation the framing already demands be surfaced), or logged as ACCEPTED-RISK in §9. With
R2-1's per-field dependency granularity + non-blocking lifecycle annotations, and R2-2's metadata
re-derivation on un-tombstone, the residual surprise shrinks to genuinely value-dependent cascades —
at which point the weakened promise is defensible. As written, it is sound but oversold.
