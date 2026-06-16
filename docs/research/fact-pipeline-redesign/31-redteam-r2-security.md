# Red-team round 2 — Security, RLS & domain-firewall leakage

**Lens:** SECURITY / RLS / DOMAIN-FIREWALL LEAKAGE (adversarial, round 2).
**Target:** `21-spec-v1.md` (revision after R1, which landed four SEV-1s: S1 resolver
read-oracle, S2 move→undo→retime laundering, S3 location movement-oracle, S4 confused-
deputy committer). Consulted: `30-redteam-r1-security.md`, `00-framing.md` §4.
**Method:** same threat model as R1 — note author + extractor LLM + a compromised/steered
`agent` actor are adversaries; a fatigued owner clicks through; a `general`-scoped surface
is the exfil sink. R2 has two jobs: (A) try to DEFEAT each R1 mitigation as written; (B)
attack v1's NEW surface (the resolver's "blindness," the global attribute-free index, the
one-way-purge destroy path, the both-domain audit redaction, and snapshot-undo / human-op
overlay as committer bypasses).

**Severity:** SEV-1 = firewall breach or unauthorized cross-band write · SEV-2 = exploitable
leak/integrity/destruction defect, bounded or precondition'd · SEV-3 = nit/hardening gap.

**Headline:** three of the four R1 SEV-1s are genuinely closed. **S2 (laundering) is NOT —
it is reborn through *derivation before undo*: v1 forbids resurrection-ops *on the move-
lineage row* but never forbids copying the now-readable general value into a sibling
general row that carries no lineage flag.** That is R1-S2's own stated root cause ("downstream
derivation," migration M4) surviving the (iii) one-way fix. Two NEW SEV-2s: the one-way
purge is an irreversible destroy/desync path, and snapshot-undo restores a *frozen*
`domain_code` without re-running the committer's firewall re-derivation. The accepted-risk
global index is a real (SEV-2) inference oracle, not a nit.

---

## Part A — verification of the R1 SEV-1 mitigations

### S1 (resolver read-oracle) — CONFIRMED-CLOSED *for attribute reconstruction*; residual existence-bit folded into NEW-2

**v1 mitigation (§6.3):** resolver returns opaque match/no-match only; constant-work decoy-
padded full candidate scan (no early-exit); no rank/confidence into general rows;
`candidate_ids` general-only; rate-limited; audited in both domains; operates over the
attribute-free global index (§3.5).

**Defeat attempt:**
- *Attribute reconstruction* (the original R1 chain — binary-search a health alias): genuinely
  closed. With no attribute returned, no rank signal in any general row, and constant work,
  there is no per-character oracle over the protected display string. The alias can't be
  reconstructed. Good.
- *Timing:* "constant-work over the full candidate set" is only constant if the candidate set
  is itself attribute-independent. A real ANN/blocking-key index buckets candidates; bucket
  occupancy is a function of how many canonicals (incl. protected) share the key — so "full
  candidate set" is either O(all entities) (implausible on the ingest hot path the index was
  *added* to speed up — §3.5/perf S3-1) or it is bucket-scoped and the bucket size is a
  protected-cardinality signal. **The spec asserts constant-work but the same §3.5 sells the
  index as a latency win; those are in tension.** Hardening: pad to a fixed candidate budget
  per query independent of bucket occupancy, and prove it in S-test 5(b) against a corpus
  where the protected side has 1 vs N co-bucketed canonicals. Marked as a residual, not a
  standalone SEV here because R1-S5's relink-footprint path and S3's association-gate already
  re-expose the *existence bit* — see **NEW-2**.

**Verdict: CONFIRMED-CLOSED** for the read-oracle as filed; the surviving 1-bit existence /
cardinality channel is reclassified and graded under NEW-2 (SEV-2).

### S2 (move→undo→retime laundering) — **STILL-OPEN (re-graded SEV-1)** — see NEW-1

**v1 mitigation (§6.4/§7f):** one-way downgrade; undo PURGES G's `value_json` (tombstone);
`retime`/`unretract`/`supersede` forbidden on any row with `lineage_op_kind='move_domain'`.

**Defeat (succeeds):** the forbidden-op guard keys on the lineage flag carried by **G**, the
copy-forward row. But between the move and the undo, G is a **legitimate, readable general
row**. Nothing forbids `add_fact`, `split_fact`, or `merge_facts` from reading G's value and
writing a **sibling general row S that carries no `move_domain` lineage** (it's a fresh
general fact, attribution `human_assertion`/`extracted`). Then undo the move: G is purged, P
restored — but **S survives**, carrying the protected value, with lineage pointing at a
correction op, not at finance/health. The both-domain move audit does not re-fire (S is a
pure intra-general write). This is exactly R1-S2's root cause ("a retained general row is a
re-attachable handle" → generalized to "any value copied out before purge"), and the (iii)
one-way fix addressed only the *named* row, not derivation off it. **Full chain + fix in
NEW-1.**

### S3 (location movement-oracle via add_fact) — CONFIRMED-CLOSED (write path)

**v1 mitigation (§6.2/§4.4):** no general fact may associate its object to a protected
`canonical_id` except via owner-gated `identity_merge`; `agent`-sourced `add_fact` barred
from cross-canonical association; committer checks footprint.

**Defeat attempt:** the write that *bound a general edge to a location canonical* is now
rejected for agent/extractor and gated for owner — the emergent-oracle fixture (S-test 7) is
unconstructable from a general/agent principal. An adversary can still author many
`Me—visited→<minted general place>` edges, but those reference **general-only** projections
the adversary supplied; no location row informs them, so there is no leak (the adversary
learns nothing it didn't author). **Confirmed-closed.** The *cost* is that the footprint
check imports the S1 existence-bit oracle (NEW-2) — the mitigation is sound but relocates the
residual.

### S4 (confused-deputy committer on add_fact) — CONFIRMED-CLOSED

**v1 mitigation (§6.1/§4.4):** committer re-derives `domain_id` from operands and **fails
closed** when it cannot derive from a non-op-controlled operand; `add_fact` must cite a real
in-scope note + an existing subject projection; minting subject AND claiming domain in one op
is forbidden; attribution non-droppable + indexed.

**Defeat attempt:** the R1 chain (mint subject + op-as-provenance + claimed `general` domain)
is now rejected at validation — both re-derivation anchors are required to be real, and fail-
closed removes the "fall back to claimed domain" step. A general agent citing a *general* note
+ *existing general* subject and a value it happens to hold commits a general fact — but the
committer reads no protected row to do so, and the value's protected origin (if any) is a
pre-existing breach outside this op's surface. The op cannot itself move a protected value
across a band. **Confirmed-closed.** (Note S4's residual — an agent that *already* holds a
protected value — is the laundering-persistence problem, addressed for the move path by NEW-1;
add_fact attribution being non-droppable keeps it watch-metric-visible.)

---

## Part B — NEW findings against v1's surface

### NEW-1 — [FINANCE/HEALTH] `move_domain` value laundered via derivation *before* undo (one-way purge insufficient) — **SEV-1**

**Severity: SEV-1 (protected value persists general-live; move no longer forensically
attributable).** This is R1-S2 resurrected; v1's (iii) fix is necessary but incomplete.

**Attack chain:**
1. Owner (or an injected review card that pre-fills the confirm — note §6.4(c) requires a
   non-pre-filled confirm, but the *post-move* steps need no confirm) downgrades one finance
   fact to general: G minted, P marked `superseded`, both-domain audit fires, lineage flag set
   on G.
2. While G is general-live and readable, a general-scoped `agent`/`extractor`/human op issues
   **`add_fact` (citing G's note + an existing general subject) OR `split_fact`/`merge_facts`
   over G's span**, producing sibling row **S** whose `value_json` carries the finance value
   but whose `lineage_op_kind` is null/`add_fact` — **not** `move_domain`.
3. Undo the move. G's `value_json` is purged (tombstone), P restored. The forbidden-op guard
   (`retime`/`unretract`/`supersede` on lineage rows) never fires — no op touched G's lineage
   row; S was derived *from* G, it is not G.
4. Net: S is general-live with the finance value; finance shows P superseded→restored; the
   both-domain audit recorded only the original move, which now appears *reverted*. The value
   is laundered and the audit trail says it was un-done.

**Root cause:** the (iii) guard is **row-scoped to the lineage flag**, but copy-forward's
whole premise is that G holds a *readable* general value; any read-then-write before purge
escapes the flag. R1-S2's root cause was "a retained general row is a re-attachment handle" —
v1 purged *that* handle on undo but left the window in which the value is copyable, and copies
don't inherit the flag.

**Mitigation:**
- **Propagate the `move_domain` lineage taint transitively.** Any op whose operand reads a
  `lineage_op_kind='move_domain'` row (G) must stamp the *derived* row with the same taint
  (`derived_from_move`), so S is also non-resurrectable / purged on the source move's undo and
  is barred from the same op set. Compute the taint at commit from operand lineage, not from
  the op kind.
- **Quarantine window:** forbid `add_fact`/`split_fact`/`merge_facts`/`replace_member` that
  *read* a move-lineage row until the move is past an undo horizon (or make such derivation
  itself owner-gated + both-domain audited, like the move).
- Extend **S-test 4** to `{move → add_fact(from G) → undo}`, `{move → split_fact(G) → undo}`,
  `{move → merge_facts(G,·) → undo}`: assert the derived row is purged/tainted and not
  general-live, and that the derivation is both-domain audited.

### NEW-2 — [ALL FIREWALLS] The attribute-free global index is a co-membership / cardinality inference oracle (accepted-risk is actually SEV-2, not a nit) — **SEV-2**

**Severity: SEV-2 (1-bit-per-query protected-EXISTENCE + set-membership + cardinality leak;
rate-limited + audited, so bounded — but real, and v1 under-rates it as a mitigated residual).**

v1 §8 books this as ACCEPTED-RISK ("high-value asset — mitigated to attribute-free +
constant-work + audited, not eliminated") and §7(l)/§9 treat the resolver as closed. **It is
not benign.** The index is attribute-free, but it is keyed by `canonical_id` and *spans all
domains by construction* (that is the point of §3.5). Three channels survive every S1
mitigation because they don't need an attribute:

- **Co-membership / existence bit:** the S3/§6.2 association gate and the §6-allowlist relink-
  footprint check (R1-S5) BOTH require the committer to answer "does this canonical have a
  protected projection?" That answer is a **1-bit protected-existence oracle returned into the
  general commit path** (commit-vs-route-to-review is observable to the general session).
  Binary-search over crafted general mentions resolves *which general entities are also
  health/finance/location entities* — set membership of the protected band — without reading a
  protected attribute. This is the movement/relationship-graph leak R1 feared, re-expressed at
  the identity layer.
- **Cardinality:** see S1 timing residual (Part A) — bucket occupancy / "full candidate set"
  size correlates with protected co-membership.
- **The both-domain audit as a *general-side* signal:** the resolver audits in both domains;
  the general session can't read the health audit row, but rate-limit accounting and the
  general-side audit existence are shared state — the throttle that trips is itself an oracle
  on query volume against protected buckets.

**Why it matters for the framing question:** v1's stated accepted-risk ("the global resolution
index is a high-value asset, mitigated not eliminated") is **NOT truly acceptable as written**
— but it is **SEV-2, not SEV-1**, because what leaks is existence/membership/cardinality at
≤1 bit/query under a rate limit + audit, never a protected *value* and never an unauthorized
*write*. It clears the SEV-1 bar (no firewall breach, no cross-band write) but fails the "no
new SEV-2" success criterion. It should be re-booked from "accepted residual" to an explicit
SEV-2 with a mitigation owner.

**Mitigation:**
- Make the existence-bit answer **owner-gated, not a committer convenience**: the "does this
  canonical have a protected projection?" check must run as an owner-authenticated, both-domain-
  audited resolver call with its own rate budget — never silently inside a general-scoped commit
  whose accept/route outcome the general session observes. For agent/extractor ops, the gate
  resolves to **route-to-review unconditionally** (no per-canonical branch the session can read).
- Fixed per-query candidate budget independent of bucket occupancy (kills cardinality/timing).
- A per-session *cross-domain-resolution budget* that, when exhausted, fails identically to
  no-match (constant failure mode), and alarms the owner.

### NEW-3 — [INTEGRITY/AVAILABILITY] One-way purge is an irreversible DESTROY + a potential cross-band desync — **SEV-2**

**Severity: SEV-2 (irreversible data destruction and a possible firewall-state inconsistency
between the generic snapshot-undo and the §6.4 move-specific purge).**

**Two problems:**
1. **Destroy / tamper.** §6.4 makes undo of a move PURGE `value_json` with "re-protecting
   requires authoring a new fact." An adversary who can trigger an *undo* (an injected review
   card, a steered agent batch-undo at §4.5 batch granularity, or a mis-targeted cascade per
   §1.2) **permanently destroys** the moved value — there is no inverse. Combined with batch
   undo (M11), one undo can purge a whole sub-batch of moved values irreversibly. Undo is
   normally the *safe* operation; for move-lineage rows it is the *destructive* one, and the
   spec does not gate *who* may undo a move or require the same owner-confirm the forward move
   required.
2. **Cross-band desync.** The generic undo (§1.2) "tombstones the assertions a target op wrote
   and **un-tombstones the ones it superseded**." The move superseded P (protected). But §6.4
   says move-undo "PURGES the general row's `value_json` (tombstone), **not** retract." These
   two rules must compose to: G purged AND P un-superseded/restored. The spec does not state
   that P is restored on move-undo — §6.4 describes the general-side action and is silent on
   the protected side. If P is left `superseded` while G is purged, **the value is destroyed in
   *both* bands** (tamper). If instead P is restored but G's purge is partial (audit/`object_id`
   retained), a re-attachment handle survives (feeds NEW-1).

**Mitigation:**
- **Undo of a `move_domain` requires the same owner-gated, non-batchable confirm as the
  forward move**, and is excluded from agent/extractor and from blanket batch-undo; show "this
  permanently destroys the general copy and restores the protected original."
- **Specify the protected-side action explicitly:** move-undo MUST un-supersede P atomically
  with purging G, in one transaction, asserted by an isolation test (extend S-test 4: after
  move→undo, P is live-protected AND G carries no resurrectable value AND no general row holds
  the value).
- Distinguish "purge" (value gone, irreversible) from "restore" (P live again) in the audit so
  the both-domain view shows destruction vs reversal.

### NEW-4 — [INTEGRITY] Snapshot-undo restores a FROZEN `domain_code`, bypassing the committer's live firewall re-derivation — **SEV-2**

**Severity: SEV-2 (un-tombstone is a write that re-instates a row's domain from frozen
`resolved_outputs` without re-running the §6.1 domain re-derivation; exploitable when identity
topology changed between write and undo).**

**Chain:** §1.2/§3.4 freeze each op's `resolved_outputs` (incl. resolved entity ids,
cardinality stamp) and §3.1 stores `domain_code` on the assertion. Undo "un-tombstones the
ones it superseded" by `op_id`+`supersedes`, replaying *recorded outcomes, never re-deriving*
(§1.2 explicitly). So un-tombstoning restores the row with its **frozen `domain_code`** and
**frozen `subject_id` (a same-domain projection)**. If, between the original write and the
undo, an owner `identity_merge` / `move_domain` / `split_entity` changed the subject's domain
topology (e.g., the projection the row pointed at was redirected, or the canonical gained/lost
a protected projection), the restored row's frozen domain may **no longer match what the
committer would re-derive today**. Undo is the one write path that, by design, **skips** the
§6.1 fail-closed re-derivation — it trusts the freeze. A carefully ordered
`write → identity change → undo` can reinstate a row whose domain is now wrong, or whose
subject projection now crosses a firewall it didn't at write time.

**Why it's only SEV-2:** it requires the adversary to drive an intervening identity op (owner-
gated) and an undo, and the inconsistency is between *frozen* and *current* topology rather
than a direct cross-band value read — bounded, but it is a write that escapes the firewall
chokepoint the whole design rests on (§6 "a human edit and a model proposal traverse the
identical committer + RLS path" — undo does NOT).

**Mitigation:**
- Undo's un-tombstone must **re-validate the restored row against the committer's *current*
  domain re-derivation and RLS `WITH CHECK`**, not just replay the frozen `domain_code`. If the
  re-derived domain differs from the frozen one, **block the undo and route to review** (the
  topology changed; a blind restore is unsafe) — consistent with §1.2's existing "block or
  cascade" posture, extended from data-dependency to *domain*-dependency.
- Add an isolation test: `write(row r, domain X) → identity_merge/move that changes r's domain
  topology → undo` must NOT silently reinstate r with stale domain X; assert block-or-review.

---

## Disposition summary

| R1 SEV-1 | v1 mitigation | R2 verdict |
|---|---|---|
| S1 resolver read-oracle | attribute-blind/constant-work/rate-limited/dual-audited resolver | **CONFIRMED-CLOSED** (attribute recon); existence-bit residual → NEW-2 |
| S2 move→undo→retime launder | one-way + purge-on-undo + lineage-op ban | **STILL-OPEN** → NEW-1 (laundered via derivation before undo) |
| S3 location movement-oracle | general→protected association gated/owner-only; agent barred | **CONFIRMED-CLOSED** (write path; oracle relocates to NEW-2) |
| S4 confused-deputy committer | domain re-derived + fail-closed + real-note/subject | **CONFIRMED-CLOSED** |

**Accepted-risk ruling:** v1's "global resolution index is a high-value asset (mitigated, not
eliminated)" is **NOT acceptable as a silent residual, but it is SEV-2, not SEV-1** — it leaks
protected existence/membership/cardinality at ≤1 bit/query, never a value and never a write.
Re-book as NEW-2 with an owner-gated existence-check + fixed candidate budget; do not ship it
as an unowned accepted-risk.

**New findings:** NEW-1 (SEV-1), NEW-2 (SEV-2), NEW-3 (SEV-2), NEW-4 (SEV-2).
This round does NOT clear the §5 success bar (one new SEV-1 + three SEV-2); a v2 revision is
required.

*The propose/commit chokepoint, projections, and three of four R1 fixes hold. The firewall now
fails at the seams the fixes created: copy-forward's readable window (NEW-1), the purge's
irreversibility/desync (NEW-3), undo's frozen-domain replay (NEW-4), and the identity index's
unavoidable existence bit (NEW-2).*
