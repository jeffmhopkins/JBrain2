# Inline Approvals — Build Plan

> **Status:** In progress · **Last verified:** 2026-07-13 · **Waves:** W1✅ W2◻️ W3◻️

**A scheduled build plan** (per `docs/DOC_LIFECYCLE.md`). It moves Proposal approval
**out of the side panel and into the conversation** — an interactive inline component
that shows the staged diff, takes approve / decline-with-reason / correct-in-place in
place, and — the net-new capability — **notifies the assistant of the outcome so it can
follow up**. This is the exact "DEFERRED CONCEPT" named in
`frontend/src/agent/FullBrainSurface.tsx:1068-1074` ("… AND notifies the agent of the
outcome so it can follow up … needs a backend feedback loop; it is intentionally not
built here.").

Grounded against the shipped Proposal primitive (`docs/reference/ASSISTANT.md`
"Staging & approval"; `backend/src/jbrain/agent/proposals.py`,
`backend/src/jbrain/api/proposals.py`), the Full Brain chat surface
(`FullBrainSurface.tsx`, `useFullBrain.ts`), and the data-framed-`UserMessage` precedent
in `backend/src/jbrain/api/agent.py` (the L7b location-presence prepend, lines ~548-556).
Migration numbers are a snapshot as of the `Last verified` date — the head is **0129**
(`backend/migrations/versions/`); re-derive it before building. All examples are
synthetic.

## GUI gate — satisfied

The mock-first / three-artifact GUI gate (`docs/reference/PROCESS.md` §GUI gate,
`docs/reference/DESIGN.md` §UI process) is **met**: four interactive variants were
presented (`docs/mocks/inline-approvals/{a-arm-and-enact,b-batch-tree-enact,
c-edit-and-reason,d-one-tree}.html` + `README.md`) and the owner chose **D — one tree,
per-leaf approve/decline/edit, one Enact that returns a single consolidated message**.
`d-one-tree.html` is the **binding spec** for the frontend surface.

---

## 1. Goal & scope

**Goal.** When the agent stages a Proposal mid-conversation, the owner acts on it
**in the transcript** and the agent **learns the outcome** in the same chat, closing the
loop the panel never had — without weakening the Proposal privilege model (each approval
still authorises one bounded operation, run by the trusted executor under the owner's
hand; `docs/reference/ASSISTANT.md` "Staging & approval").

**In scope:**
- The **enact→agent feedback loop**: enact returns a **server-authored** outcome summary;
  the assistant receives it as a **data-framed turn** and follows up.
- **Correct-in-place**: edit a leaf's proposed value before enact (generalises
  `patch_intake_config` to `add_note` / `manage_appointment` bodies); the edit flows
  through `agent_note_executor` (it reads `preview.body` at enact time).
- **Decline-with-reason**: a per-node reason captured at decline, persisted, and folded
  into the outcome the agent sees.
- The **inline component** (variant D) at the single chip render-site
  (`FullBrainSurface.tsx:530,636`); the **side panel is retained for browsing older /
  cross-session proposals** (it is no longer the way to act on the one in front of you).
- Mock states + component tests; backend unit + integration + RLS tests; `.tool` sidecar
  refresh; docs reconciliation.

**Out of scope (named follow-ons):**
- **Server-side turn orchestration** (the enact endpoint spinning its own `AgentLoop` /
  `_LiveTurn` broker) — see the Decision below; the frontend-initiated follow-up reuses
  the settled chat machinery and is the chosen path.
- Editing **structural** node fields (subject, domain, predicate) — firewall fields stay
  non-editable, exactly as intake's `_EDITABLE_INTAKE_FIELDS` excludes them.
- The **wiki-restructure** multi-op tree keeps its existing panel tree for now; the inline
  card targets the common single-/few-op case (correction, appointment, medication,
  reminder, egress). The tree renderer is shared, so wiki-restructure can graduate to
  inline later with no new paradigm.
- Cross-device / persisted "outcome sent" state — the follow-up turn is itself the record.

---

## 2. Binding behaviour (from `d-one-tree.html`)

One Proposal renders as one inline card in the answer bubble. Every operation is a leaf:

- **Approve (✓)** — default state is all-approved (matches the mock and the panel).
- **Decline (✕)** — opens a per-leaf reason chooser (chips + optional note); the leaf
  strikes through and drops out of the enact set.
- **Correct in place** — tap the value to edit; the leaf turns **corrected** (amber, "·
  edited") and enacts the owner's value, filing it as a correction.
- **Held** — an approved leaf whose prerequisite is declined shows **held** and is not run
  (fail-closed, exactly `enactment_plan`'s `enactable` vs `held`).
- **One Enact** at the foot — a **double-tap** (arm → "tap to enact N" → run). It runs the
  approved, unblocked leaves and posts **one consolidated outcome** to the assistant, e.g.
  *"Enacted 3 of 4 — 2 approved, 1 corrected (HCTZ → 25 mg) · declined 1 (reschedule:
  wrong date). Returned to assistant as 3 approvals."*

---

## 3. Architecture

### 3.1 The enact→agent feedback loop (the headline)

**Chosen: server-authored outcome + frontend-initiated data-framed follow-up turn.**

1. `POST /proposals/{id}/enact` runs the executors (unchanged) and now also **builds a
   server-authored outcome summary** from the enacted/held leaves + their labels and
   returns it: `EnactOut{ enacted: [...], held: [...], outcome: str }`. The string is
   built from DB truth (which leaves actually enacted), never model text — honest by
   construction, and it is the artefact the agent will see.
2. The frontend, after a successful enact, calls `fb.send(outcome, { proposalOutcome:
   true })`. Enact runs while `busy` is false (the staging turn already settled), so
   `send` is the correct hook (it early-returns only when busy).
3. `ChatRequest` gains an optional `proposal_outcome: bool`. When set, `chat()` frames the
   message as a **data `UserMessage`** on the conversation channel — the same
   data/instruction boundary the L7b presence block uses (`api/agent.py` ~548-556) — and
   marks the transcript turn as an outcome report (right-aligned "returned to assistant"
   chip, per the mock), not owner prose. Invariant #1 (data-not-instruction) holds: the
   text is a coarse, owner-triggered outcome report, framed as data.

**Why not server-side turn injection.** Having the enact endpoint spin its own
`AgentLoop` + run-log + `_LiveTurn` broker to drive a turn into the originating session
duplicates the whole detached-turn machinery for no user-visible gain — the frontend is
already attached to that session and `fb.send` reuses it. Rejected as scope the loop
doesn't need. (The proposal's `session_id` still gets surfaced — see W1 T1 — so a future
server-side path, or a background/system proposal enacted from the panel, has the routing
it needs.)

### 3.2 Correct-in-place

Generalise `ProposalRepo.patch_intake_config` (`proposals.py:360-387`) to
`patch_node_body(node_id, body)` guarded to `op IN ('add_note','manage_appointment')` and
`p.status = 'staged'`. New endpoint `POST /proposals/{id}/nodes/{node_id}/edit
{ body: str }`. `agent_note_executor` already reads `preview.body` at enact time and is
idempotent on `client_id = proposal-{node.id}` (not body), so an edited body produces a
corrected note with no duplication risk.

### 3.3 Decline-with-reason

Migration **0130** (re-derive head) adds `decision_note text` (nullable) to
`app.proposal_nodes` (the `status` CHECK already allows `rejected`). `DecisionIn` gains an
optional `reason: str | None`; `decide_node` / `ProposalRepo.decide` thread it onto the
rejected node. New table column → **RLS isolation test** (non-negotiable #3). The reason is
owner-eyes feedback, folded into the enact-outcome summary the agent sees.

---

## 4. Decisions (settled — owner sign-off 2026-07-13)

1. **Feedback-loop mechanism** — **frontend-initiated, server-authored outcome** (§3.1).
   Server-side turn injection rejected as duplicated machinery.
2. **Correct-in-place provenance** — an owner-edited node enacts as **`provenance='human'`**
   with `source_ref='proposal:{id}#edited'` (the owner authored the final text; the #7
   human-correction channel, honest attribution + normal human weight). An **un-edited**
   approved node keeps `provenance='agent'` as staged.
3. **Decline-reason persistence** — **persisted** on the node (§3.3, `decision_note`),
   auditable + RLS-tested, and folded into the outcome the agent sees.
4. **Armed color** — **green** for the Enact confirm (a save, per the green=save rule),
   **rose** only for the decline-confirm.

---

## 5. Waves

### W1 — Backend: the feedback loop, correct-in-place, decline-reason ◻️

Parallelizable tasks off one `wave-1` branch:

- **T1 · Enact outcome + session routing.** Widen `load`'s SELECT + `ProposalRow` with
  `session_id` (`proposals.py:277,294`). Add `EnactOut.outcome` (server-authored summary
  from enacted/held labels + counts). Unit + integration tests (`test_proposals_api.py`,
  `test_agent_proposals_pg.py`).
- **T2 · Data-framed outcome turn.** `ChatRequest.proposal_outcome: bool`; `chat()` frames
  the message as a data `UserMessage` + marks the transcript turn. Unit test the framing;
  integration test the round-trip (LLM faked via `jbrain.llm.fixtures`).
- **T3 · Correct-in-place.** `patch_node_body` + `POST .../nodes/{id}/edit`; provenance per
  Decision #2. Unit + integration (edited body → corrected note); reuse
  `agent_note_executor` idempotency test.
- **T4 · Decline-reason.** Migration 0130 (`decision_note`), `DecisionIn.reason`, thread
  through `decide`; **RLS isolation test** for the new column. Fold reason into T1's outcome
  summary.
- **T5 · `.tool` sidecars.** Update `propose_correction.tool`, `manage_appointment.tool`,
  `propose_merge.tool`, connector egress tool copy: the enact outcome now returns to the
  agent (it can follow up); a staged node's value may be owner-corrected before enact. Bump
  `version:` and update the pinned digests in
  `backend/tests/unit/test_agent_readtools.py` (`pins` dict).
- **Docs (travels with W1):** `docs/reference/ASSISTANT.md` — the closed enact→agent loop,
  correct-in-place at enact, decline-reason.

**DoD:** ruff + pyright clean; unit tests green locally; integration/RLS tests written (run
in CI); coverage ≥80 with the security-touching paths (edit endpoint, decision-reason,
RLS) at 100%; digests re-pinned; ASSISTANT.md reconciled + `Last verified` bumped.

**Landed 2026-07-13** — built, then gated by an independent adversarial review (reviewer
≠ builder). No correctness bugs found; the review's coverage findings were all fixed on
this branch: an RLS isolation test for the edit path, an end-to-end edited-leaf →
`provenance='human'` enact test, a decline→approve reason-cleared test, a defensive
"only a truthy `edited` upgrades provenance" test, and a summary honesty fix (a still-
`pending` leaf is now reported "left undecided", with a test). T5 was extended to the two
egress connector tools (`lookup_medication` v2, `lookup_condition` v2) so all
inline-targetable staging tools name the outcome loop. **Deferred (completeness, not a
hole):** a full faked-LLM `/chat` round-trip of the `proposal_outcome` turn — the pure
`_model_message` framing (the security-relevant part) is unit-tested; the round-trip is
exercised for real in W2's mock `/api/chat` route.

### W2 — Frontend: the inline approval component ◻️

Depends on W1 endpoints. Off one `wave-2` branch:

- **T1 · `InlineProposal` component** (variant D) rendered at the chip site
  (`FullBrainSurface.tsx:530,636`): grouped tree, per-leaf approve / decline-with-reason /
  correct-in-place edit, held propagation, one double-tap Enact. Reuse `node-row/status-*`,
  `badge.warn` (held), `badge.bad` (declined), `seg`, `enact-result`, `rail-armed`; new
  `.fb-inline-proposal` scoped under `.fb-shell`. Tokens-only, ≥44px, reduced-motion.
- **T2 · Wiring.** `types.ts`: `Decision`/decide path carries `reason?`; add `editNode`;
  `EnactResult` gains `outcome`. api client: `editNode`, `decideNode(reason?)`, enact reads
  `outcome`. On enact success → `fb.send(outcome, { proposalOutcome:true })` (thread `fb`
  into the card; `send` gains the `proposalOutcome` opt → `ChatRequest.proposal_outcome`).
- **T3 · Chip → older-proposal nav.** The `ProposalChip` stays as a secondary "open in
  panel" affordance for older/cross-session proposals; the inline card is the act-in-place
  surface for the current turn's proposal.
- **T4 · Mock states + tests.** Prop-injected fixtures (matching `ProposalTree.test.tsx`
  DI) for default / empty / held / declined / edited / error / offline; add `/api/proposals*`
  + a minimal `/api/chat` SSE route to `mock.ts` so `dev:mock` exercises the round-trip.
  vitest + testing-library component tests (arm→enact, held, decline-reason, edit-flip, one
  outcome send).
- **Docs (travels with W2):** `docs/reference/DESIGN.md` — the inline-approval surface
  decision (variant D binding); update "Full Brain lateral shortcuts" (panel = browse
  older/cross-session; inline card = act-in-place). `docs/mocks/inline-approvals/README.md`
  status → chosen/shipping.

**DoD:** biome + tsc clean; vitest green; all mock states present; DESIGN.md reconciled +
`Last verified` bumped.

### W3 — Reconcile & land ◻️

- Flip this plan `Scheduled → Shipped`, move to `docs/archive/`, drop the `plans/README.md`
  row, update the `ROADMAP.md` assistant section, bump `Last verified` dates. `dev-setup.sh`
  reviewed (no new dep expected — zero-new-dep goal).

---

## 6. Test & gate obligations (binding)

- **Per task (local):** `ruff`/`pyright` or `biome`/`tsc` + the task's unit tests before it
  merges to the wave branch (`docs/reference/PROCESS.md` §Verification).
- **Per wave (CI, at the PR):** full suite — lint, typecheck, testcontainers integration,
  `--cov-fail-under=80`, security paths 100% (edit endpoint, decision-reason, every new
  RLS-scoped path), `.tool` digest pins, `dev-setup.sh` currency, `scripts/docs-freshness.sh`.
- **RLS:** the new `proposal_nodes.decision_note` column needs an isolation test
  (non-negotiable #3). No new table; the firewall fields (subject/domain) stay non-editable.
- **One PR per wave**, opened only after both review gates (per-task + per-wave adversarial,
  reviewer ≠ builder) are clean.

---

## 7. Rollout

Backend (W1) ships behind no flag — the new `outcome` field and `proposal_outcome` flag are
additive and ignored by the current chip UI. W2 swaps the render site to the inline card;
the panel and its endpoints are untouched, so a regression reverts to the chip by reverting
one render site. No data migration beyond the additive `decision_note` column.
