# Inline approvals — moving Proposal approval into the conversation

> **Status:** GUI gate settled — **owner chose variant D** (`d-one-tree.html`), the binding
> spec, now **shipped** (`docs/archive/INLINE_APPROVALS_PLAN.md`; backend + `agent/InlineProposal`).
> A–C retained as the record.

## The problem

Today a staged `Proposal` surfaces in chat only as a navigational **"Review
proposal"** chip; approving it means leaving the conversation for the
**Proposals side panel** (swipe-left from the Full Brain composer, or the
launcher tile). The panel is good for *browsing* the queue but heavy for the
common case — one proposal, staged mid-conversation, that you want to accept or
reject without losing your place. The code already names this gap as a
**deferred concept** (`frontend/src/agent/FullBrainSurface.tsx`): *"an
interactive inline component that shows the proposal's diff, takes
approve/reject in place, reflects live state, AND notifies the agent of the
outcome so it can follow up."*

## What these mocks propose

Approval becomes an **expandable component inline in the transcript**. The
interaction is a **double-tap**, reusing the repo's settled
tap-again-to-confirm doctrine: the first tap **arms** the choice (the button
fills and reads *"tap to enact"*), the second **enacts**. Nothing fires on a
single tap, so a stray touch never writes.

On enact, a **message is sent back to the assistant** — rendered inline as a
sent chip (e.g. *"Enacted — 1 approval"*, *"Declined — reason: wrong dose"*) —
closing the feedback loop the panel never had, so the agent can follow up in the
same turn.

The **side panel is kept only for reviewing older / cross-session proposals**;
it is no longer the way you act on the one in front of you.

All three are standalone, interactive, dark-theme, phone-width, and built on the
DESIGN.md token sheet (no raw hex, ≥44px targets, `prefers-reduced-motion`
honored). Domain/semantic color stays informational: steel = info/agent, green =
approve/done, amber = pending/held/edit, rose = decline/danger, health = rose.

## The three variants

| | File | Interaction model | Best for |
|---|---|---|---|
| **A** | `a-arm-and-enact.html` | Per-card **arm-and-enact**: expandable card, two always-visible buttons (Decline · Approve), each a double-tap. | The common single-op case — health correction, add reminder, record medication. The most literal reading of the request. |
| **B** | `b-batch-tree-enact.html` | **In-chat tree + batch enact**: a multi-op Proposal renders as the approvable tree inline (per-leaf ✓/✕, dependency **held** badges), with one footer **Enact N** as the double-tap. Sends *"enacted N approvals."* | Multi-operation proposals — `wiki-restructure`, and the `egress` "approve the exact outbound payload" flow. Brings the side-panel tree inline without the swipe. |
| **C** | `c-edit-and-reason.html` | **Correct-in-place + reason-to-decline**: Approve is the double-tap; tapping the value **edits it in place** (flips the primary to *Approve correction*, files a correction note with your fix); Decline opens a **reason** chooser (chips + note) so the agent learns *why*. | Fact corrections and appointment changes where yes/no is too blunt — the owner tweaks the value or explains the rejection. |
| **D** | `d-one-tree.html` | **C's richness under B's one tree** — the current direction. Every operation is a leaf you **approve (✓) / decline (✕ → reason) / correct in place** (edit a value → the leaf turns *corrected*). Held-dependency rules apply. **One Enact** (double-tap) at the foot runs the approved, unblocked leaves and returns **a single consolidated message** to the assistant (e.g. *"Enacted 3 of 4 — 2 approved, 1 corrected (HCTZ → 25 mg) · declined 1 (reschedule: wrong date). Returned as 3 approvals."*). | The whole staged plan in one card, one decision surface, one round-trip to the agent. |

**D is the consolidation** of the round — it takes C's per-item edit/reason and
puts it under B's single tree with **exactly one Enact and one return message**,
rather than a separate approve/enact per card.

Each mock stages **several** proposal kinds in one conversation so the pattern is
visible across `correction`, `reminder`, `medication`, `wiki-restructure`,
`egress`, and `appointment`.

## How the pattern maps to the real system

- **Kinds & glyphs** match `frontend/src/agent/types.ts` /
  `ProposalsPanel.tsx` (`correction ✎`, `wiki-restructure ⤴`, `merge ⧉`,
  `appointment ◷`, `egress ↗`, …).
- The **tree + held/dependency** logic mirrors the settled
  `docs/mocks/assistant-proposals-view.html` and
  `backend/src/jbrain/agent/proposals.py` (`enactment_plan` → enactable / held).
- **Correct-in-place** and **reason-to-decline** reuse the review-inbox
  paradigms already in DESIGN.md (the inline editor files a **correction note**,
  the #7 channel — the wiki/graph stays machine-written; a human never edits a
  fact by hand).
- The **agent-notification on enact** is the net-new behavior these mocks are
  built to justify — the backend feedback loop noted as deferred in
  `FullBrainSurface.tsx`.

## Open questions for the owner

1. Which interaction model (or blend) — A's per-card arm, B's batch-enact for
   trees, C's edit/reason richness? They compose: A for single ops, B for trees,
   C's edit/reason as an option on any fact-bearing card.
2. Should the inline card **replace** the "Review proposal" chip entirely, or
   coexist (chip for a quick "open in panel", inline card for act-in-place)?
3. Exact wording of the message sent back to the assistant on enact/decline.
