# The silent queue — GUI mocks

> **Status:** Research · **Last verified:** 2026-09-08 · **Backs:**
> `docs/research/agent-ingest/F2-SILENT-QUEUE.md`

Three interactive mocks for the **silent question queue** in the agent-ingest redesign
(the deterministic note-analysis pipeline is deleted; a note becomes turn 0 of an agent
conversation; the agent commits what it is confident about and **asks the owner** about the
rest, into a queue with **no push notifications** — the owner's binding decision).

Nothing here is built. These exist to make the dossier's argument checkable, and to satisfy
the `docs/reference/DESIGN.md` §UI process gate (3–4 distinct variants, owner picks, the
chosen one becomes the binding spec) if and when the redesign is scheduled.

All three are standalone, phone-width, dark-first with a theme toggle, and built on the
DESIGN.md token sheet (no raw hex outside `:root`, ≥44px targets). All content is synthetic.

| | File | What it is | The claim it tests |
|---|---|---|---|
| **A** | `a-ambient.html` | No queue screen. A question renders **on the object it is about** — an unresolved predicate row on the entity page, an "I wasn't sure about" block above a note's committed facts, and one dismissible line at the foot of the home stream. | **The queue should not be a destination.** Owner attention is the ranking signal; only in-context delivery can read it. Answering costs one tap on a screen they opened for their own reasons. |
| **B** | `b-deck.html` | The **minute deck** — the top five questions, one at a time, big thumb targets, a hairline that shows the end coming, and an end card that says out loud what happened to the other 132. | A finite deck that **ends** beats a list that accrues. The counter must read *2 of 5*, never *2 of 137*. |
| **C** | `c-stacks.html` | The completionist list, reached from the launcher tile. Grouped by entity / **shape** / time; twelve same-shape questions collapse into **one card asked once**, with subjects as chips you flick out, then one armed batch answer. | **Batching is a grouping problem, not a selection problem.** The stack does the grouping the owner would otherwise do by reading twelve rows and ticking twelve boxes. |

## The recommendation is a composition, not a pick

**A is the spine; B is the moment; C is the fallback.** They are not three renderings of
one screen — they are three delivery channels for the same row, and the dossier argues all
three should exist with A dominant:

- **A** is where most questions are actually answered, because the owner is already there.
- **B** is the only proactive surface, and it is offered (one dashed line, once a day, top
  band only), never pushed.
- **C** exists so a completionist owner *can* drain it, and so batching has a home. It is
  deliberately hard to reach — the launcher tile, not the home screen.

Every one of them shows the same two things on every question, which is the real design
commitment: **the agent's own guess**, and **the date it will take that guess** if nobody
answers. A question in this system is never an open loop the owner owes; it is a decision
already made, with a window to overrule it.

## What each mock reuses from the shipped review inbox

- The entity grouping (`frontend/src/review/grouping.ts:57`) — verbatim in C.
- Stacked-button pick-one (`frontend/src/review/blocks/Action.tsx:171-207`), driven by
  `payload.choices` — verbatim in B's third card.
- Armed tap-again for the batch (`frontend/src/review/useArmed.ts:9-31`) — C's "Answer 12".
- The optimistic move + single undo snackbar (`frontend/src/review/useReviewQueue.ts:123-174`)
  — C's snackbar.
- Every-leaf-starts-approved, flick out the exceptions
  (`frontend/src/agent/InlineProposal.tsx:78-84`) — C's chips.
- The quiet-footer register settled by the research-expiry gate
  (`docs/mocks/research-expiry/README.md`, chosen variant A) — A's home line.

## Open questions these mocks deliberately do not answer

1. Whether the launcher badge counts the **top band** (0–5, reachable zero) or **all open**
   (the shame number). The mocks assume top band; the dossier argues it.
2. What a **health-domain** question does at its expiry date. B card 2 shows the position
   the dossier takes — firewall-domain questions **never** self-answer — but that is a
   policy the owner must ratify, because the alternative (they rot open) is the one thing
   here that can accumulate.
3. Whether "Say more…" (free text → an `owner_correction` note → re-ingest) belongs on the
   card at all, or only on the entity page where there is a keyboard and a reason to type.
