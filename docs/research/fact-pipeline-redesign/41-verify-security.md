# Final verification — SECURITY lens (consolidated, inline)

Target: `40-final-spec.md`. Focus: the newest mechanisms — §4 selective-replay arbitrary undo,
Decision 2 `add_fact`-mints-a-note — re-checked against the R2 security SEV-1s (NEW-1
derivation-laundering, NEW-4 frozen-domain-on-undo) and the two accepted-risks.

**No new SEV-1.** Three SEV-2 spec-clarifications below; both accepted-risks reconfirmed bounded.

## Findings

- **V-S1 (SEV-2) — selective replay must RE-VALIDATE frozen links against the live firewall.**
  §4 freezes typed/link resolutions for determinism but re-derives domain/firewall live. Gap: a
  *frozen link* (object `entity_id` → a same-domain projection) replayed **after** the entity's
  domain topology changed could re-materialize an edge that is now cross-domain. *Fix:* on
  replay, the committer must re-resolve each frozen link in the **current** RLS scope and apply
  the live firewall guard; a frozen link that is now cross-domain routes to review, never
  auto-commits. (Closes the only residual of NEW-4 under arbitrary undo.) Add one sentence to §4.

- **V-S2 (SEV-2) — domain_move undo under selective replay stays publish-bounded; make it
  explicit.** Undo of a `domain_move` tombstones the general copy, but any op that *derived* from
  the published value replays from its **frozen (public) output** — so the derivation stays
  public (correct: a publish is irreversible in the security sense). The risk is only if replay
  *silently* resurrects the derived value as if newly private. *Fix:* state that
  derived-from-published rows keep `provenance.kind` marking their published lineage and are
  never re-privatized by an undo; the audit shows the move reverted **and** the derivations
  retained. Confirms NEW-1's "accepted as inherent to publishing," now under arbitrary undo.

- **V-S3 (SEV-2) — `add_fact`-mints-a-note: forgery surface is closed IF two rules hold.**
  Decision 2 mints a `{user, datetime, reason}` note as provenance. Safe **provided**: (i)
  `add_fact` stays **owner-only and LLM/agent-cannot-emit** (the op-type allowlist — an injected
  note can't mint a human note); (ii) the committer **re-derives domain from the minted note's
  subject/operands**, never a claimed domain, and the fact's object must be a same-domain
  projection (no cross-firewall link via a hand-authored note). Both are already in §6 — make the
  *minted-note* path explicitly inherit them (one sentence), so a future implementer can't drop
  the guard.

## Accepted-risks — reconfirmed bounded (single-user)

- **Attribute-free global index ≤1-bit/query.** Amplifiable to set-membership only over *many*
  owner-authenticated queries; an external attacker needs an owner session to reach it; the owner
  is already authorized to see all their own domains via the resolver. Bounded covert side
  channel, documented. **Accept.**
- **Publish irreversibility.** Owner-confirmed, owner-told, audited. **Accept.**

## Verdict: **SHIP-WITH-CAVEATS.**
No SEV-1. Fold V-S1/V-S2/V-S3 (three one-sentence clarifications) into §4/§6 before the first
storage/committer PR; keep every new table's RLS isolation test (incl. the new replay-link-
revalidation test) in the same PR.
