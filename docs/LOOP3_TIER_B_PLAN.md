# Loop 3b — Tier-B durable-knowledge: contradiction-mined corrections (owner-gated MVP)

Binding single-wave plan (docs/PROCESS.md). Second half of ROADMAP **Loop 3** (the predicate-canon
half shipped in Loop 3a). The highest-blast-radius loop — it is the only one that can change
*citable truth* — so the master rule is load-bearing: **the agent never gets a privileged write
path into citable knowledge.** It survives here exactly as the shipped `propose_correction` tool
does: the agent only **stages a `correction` Proposal**; on owner approval the leaf re-enters as a
provenance-flagged, normal-weight **agent note** through normal ingestion, where the extraction +
arbiter own the graph. The agent proposes a *note*, never a fact.

Posture: **owner-gated MVP** (owner's choice). No auto-apply; the eval/groundedness-regression gate
(ASSISTANT.md #6) stays deferred (needs the replay-eval seam deferred since Loop 2).

## What it adds (vs the shipped `propose_correction` tool)

The chat tool stages corrections *in the moment*. Loop 3b is the **retrospective miner**: a nightly,
budget-gated `correction_mine` action that reads ended chat conversations and finds where the owner
**explicitly contradicted a factual claim the assistant made or cited**, then stages an owner
correction-note proposal capturing the owner's correction. High-signal (the owner literally said
it), bounded, and reuses the whole correction spine.

## Reuse (almost everything; net-new is the miner + its prompt)

- **Action shape**: mirrors `skilldistill.py` — `SelfImprovementGate` (kill-switch + real token
  budget, `record_spend`), the composite `(started_at, run_id)` HWM, batch, `_domain_of`
  (most-sensitive source scope, fail-closed), `_owner_principal_id`, the nightly-seed-migration
  pattern (0054/0055/0058).
- **Staging + enactment**: `ProposalRepo.stage` with the **existing** `kind='correction'` + a single
  `op='add_note'` leaf (preview `{body, domain}`) — the SAME shape `propose_correction` stages, so
  `build_leaf_executor`'s default `agent_note_executor` already enacts it (note provenance `agent`,
  source-attributed, NORMAL extraction weight per non-neg #7, idempotent on the node id, enqueues
  `ingest_note`). **No new proposal kind, executor, or leaf op.**
- **Router**: the contradiction judgment is one `router.complete` per candidate (the net-new cost);
  via the adapter only, faked in tests.

## The action (`correction_mine`)

1. Gate (`SelfImprovementGate.check` with a per-candidate estimate). Refuse fail-closed on
   kill-switch / exhausted budget.
2. Fetch a batch of ended chat runs past the HWM (`kind='agent'`, `status='done'`,
   `stop_reason='end_turn'`), **deduped to one per session** and requiring **≥2 user turns** (a
   back-and-forth where a correction is possible), skipping any session that already has an open
   (`staged`/`approved`) `correction` proposal from this source (idempotency).
3. For each: read the **full session transcript** (`agent_turns`, ordered by `seq`, role-labelled)
   and ask the router (data-framed, injection-resistant prompt): *did the owner correct a durable
   factual claim? If so, rewrite the corrected fact as a standalone note in the owner's voice; else
   nothing.*
4. If a correction is found, **stage** a `correction` Proposal (domain = the session's most-sensitive
   scope; provenance `{source: 'correction_mine', session_id, run_id}`). Advance the HWM; record the
   spend.

Nothing is applied — the owner reviews each proposal (body + source run) and approves; only then does
it become a note.

## Cross-cutting non-negotiables (binding)

- **Master rule**: the agent stages a *note* proposal, never writes a fact or the wiki. Enactment is
  the shipped `agent_note_executor` → normal ingestion → arbiter.
- **Data/instruction boundary (#12, the master invariant)**: the transcript is **untrusted** (user
  messages especially, but also the agent's own prose). The mining prompt frames it as DATA and
  **extracts** the owner's correction; it never executes instructions embedded in the transcript.
  The owner-gate is the backstop. **An adversarial-injection test is mandatory** (a transcript that
  tries to steer the miner must not produce an attacker-chosen proposal).
- **Untrusted-origin weight (#7)**: the staged note is provenance `agent`, NORMAL weight — never
  elevated. (The existing executor already does this.)
- **Least privilege (#8)**: the job runs at the **source session's domain** (fail-closed
  most-sensitive scope), never an escalation; a non-owner-origin session never triggers it (only
  owner chat runs exist as `kind='agent'`).
- **Firewall (#3)**: the candidate read + the stage run on RLS-scoped sessions; the proposal's
  domain = the session's scope, so a health correction is a health-domain proposal. The transcript
  read is a new query path → an RLS isolation test.
- LLM via the **router adapter only**; tests-with-code (80% / security-100% on the injection
  boundary); Conventional Commits + one PR + CI green; no new deps; `dev-setup.sh` current.
- **Seed** the nightly `correction_mine` schedule **DISABLED** (it spends budget on LLM calls + can
  propose changes to truth, so the owner opts it in deliberately).

## Red-team (resolved before build)

- **F1 — re-mining a session (duplicates).** Mining by run would re-read a multi-run session each
  exchange. → dedup candidates to one run per session AND skip sessions with an open `correction`
  proposal from `source='correction_mine'`.
- **F2 — prompt injection via the transcript.** A user/assistant line could try to make the miner
  fabricate a correction. → data-framed prompt + the output is a *staged* proposal the owner must
  approve + an adversarial-injection test asserting a steering transcript yields no attacker-chosen
  proposal (the model returns "no correction", or whatever it returns is just an owner-reviewed
  draft, never an applied change).
- **F3 — false corrections wasting owner attention / risking truth.** Owner review is the gate; the
  note re-enters *normal* ingestion (arbiter decides, normal weight), so even an approved bad note
  is arbitrated, never a direct fact write. A low-confidence judgment returns nothing.
- **F4 — budget burn on no-correction runs.** ≥2-user-turn pre-filter + small batch + HWM bound the
  nightly spend; `record_spend` meters it; the gate refuses when the day's budget is gone.
- **F5 — domain firewall.** Proposal domain = `_domain_of(session scopes)` (most-sensitive,
  fail-closed); staged under SYSTEM_CTX attributed to the owner principal; RLS test on the new read.

## Deferred (out of this MVP)

Auto-apply (needs the replay-eval + a groundedness gate); mining episodic *semantic* memory or run
analyses (start with chat transcripts — the highest-signal, owner-present source); an LLM
confidence score surfaced to the owner (the proposal body + source run is the review surface).
