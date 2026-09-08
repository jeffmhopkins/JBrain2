# The note screen under the conversation model — frozen body, clarifications, write chips

> **Status:** GUI gate **OPEN** — three variants for the owner to choose from. Nothing is
> built. **Last verified:** 2026-09-08. Plan: `docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md`
> (D3, D6, D7, D12). Companion round: `docs/mocks/agent-ingest/` (where a note's thread
> *lives* — settled on **C**, `c-unified-conversations.html`); this round is what the
> **note screen itself** becomes.

## The change these mocks are for

Two things in the ratified plan have no shipped surface, and neither is covered by the
`agent-ingest/` round:

**1 · The body freezes and grows an appendix (D6/D7).** A note keeps the body you
captured, immutable, and gains **timestamped clarification blocks** as you answer the
agent's questions. Those blocks are chunks of the same note, so the graph still
re-derives from notes alone and every citation has a real chunk (D7) — which means they
are *note text*, not chat history, and the reader has to be able to tell at a glance
**what you wrote when you captured it** from **what you added later in conversation**.
Today `frontend/src/screens/NoteScreen.tsx:79-84,421-425` renders the body as one
undifferentiated Markdown blob, so there is no existing treatment to reuse.

**2 · The "entity modified" chip (D3).** Every graph write the agent makes is visible as
a chip you expand to see what changed. There is no approve button on it — a clear fact
**commits** (D2) and disagreement is a reply — so the chip's whole job is *legibility*.
It has to carry three shapes honestly:

| shape | what the expanded state must show |
|---|---|
| a **fact write** | `subject.predicate → value`, when, and the source words highlighted |
| a **supersession** | the before→after diff, and that the old value was **closed, not deleted** (it keeps its interval on the entity's history rail) |
| an **attachment-sourced** fact (D12) | that it came from a photo/PDF and **not from your note text** — with the verbatim OCR it was read from |

All three mocks also stage an **open `ask_owner` question** and the owner answering it,
because that answer is what mints a clarification block — the two halves of this round are
one interaction.

## What they reuse (this is not new vocabulary)

- The chip is the settled **domain-dotted entity chip** + the **`StepRow` disclosure**
  (`frontend/src/agent/FullBrainSurface.tsx:1706-1746`, `:1752-1855`) — tap the row,
  detail in place, `+N more` register for overflow. Labels/inline args follow
  `frontend/src/agent/toolSummary.ts`.
- The before→after block is **`frontend/src/review/blocks/ClaimDiff.tsx`** verbatim
  (`current` struck over the new value) — the repo's only diff renderer, and the plan
  says keep it.
- Edge rows are the Analysis tab / entity-page idiom: monospace
  `subject.predicate → value`, tap to expand the citation to the **highlighted source
  words**.
- OCR is quoted the way the settled **Sources card** quotes it — a quiet monospace inset,
  `[illegible]` never reworded.
- The question card follows the inline-approval doctrine
  (`docs/mocks/inline-approvals/d-one-tree.html`): the agent proposes, the owner disposes,
  and the owner's words are filed as a **correction**, never a hand-written fact
  (non-negotiable #7).

All three are standalone, no build step, phone-viewport first, **light and dark** (they
open in the viewer's `prefers-color-scheme` and carry a toggle), built on the
`docs/reference/DESIGN.md` token sheet — no raw hex outside the token block, ≥44px
targets, `prefers-reduced-motion` honored. Colour stays informational: green = written,
amber = something was replaced / an open question, steel = provenance and agent, rose =
medical.

Every mock runs the **same scenario** so they are directly comparable — a cardiology
follow-up note (medical domain) with a photo of the new prescription, a plain BP write, a
real supersession (**amlodipine 10 mg → losartan 50 mg**, the old value closed rather than
overwritten), two attachment-only values read off the label (quantity, refills), one
clarification already answered, and one question still open (*"bloods again" — which
panel?*) that you can answer in the mock.

## The three variants

| | File | Where the conversation lives | Clarifications read as | Writes are shown |
|---|---|---|---|---|
| **A** | `a-living-document.html` | **Behind** the note — a `Thread` tab. The Note tab shows only the *product* of the conversation. | **Prose, in document flow.** The frozen body under a `captured 07:14 · your words, unchanged` seal, then an `added since` rule, then steel-ruled dated blocks — one continuous piece of writing that grew. | **Anchored to the sentence that caused them** — an indented elbow chip under each paragraph, collapsed to one line. |
| **B** | `b-note-then-thread.html` | **On** the note — the Note tab *is* the thread, body first, composer at the foot. | **Your own reply bubbles**, carrying a green `kept in the note` seal that says this one became note text (and the ones without it didn't). | **Inline, as they happened**, inside the agent turn that made them. |
| **C** | `c-note-and-record.html` | **Behind** the note — a `talk about this note` row opens the conversation as a layer. | **A separate collapsed log**: one quiet row, `3 clarifications · added in conversation`, expanding to dated blocks that each quote the question they answered. | **Collected into a ledger**, grouped by entity, on a `Record` tab that replaces Analysis. |

### What each one commits you to

- **A — the living document.** *Cost:* the agent's account of its own work is on another
  tab, so a decision you want explained is a tap away and a passing question you asked is
  nowhere in the note; and the document grows without bound — a much-clarified note pushes
  its own opening line off the screen with no fold to stop it.
- **B — the note is turn 0.** *Cost:* the note screen becomes a chat, so re-reading what
  you actually wrote is a deliberate act behind a sticky ribbon and a 3-line clamp, and
  "which of these words are mine" is answered by bubble shape rather than by structure.
- **C — the note, and the record.** *Cost:* one story in three places — the note, the log,
  the ledger — so the link between *what you said* and *what it changed* is a tap, not a
  glance, and the agent's reasoning appears on none of them.

## Open questions for the owner

1. **Does an unanswered question belong on the note screen at all**, or only in the
   `Asking` bucket settled in the `agent-ingest/` round? A and B put it in the reading
   flow; C puts it behind a tab pill.
2. **Can a clarification block be edited or withdrawn?** All three treat it as immutable
   once written (the body is frozen, and so is what you appended) — the correction path is
   another block, not an edit. If that is wrong, it changes the log's affordances.
3. **B only:** when a reply is *not* kept in the note, is the un-sealed bubble's
   `keep this in the note` escape hatch worth having, or does an explicit affordance
   invite second-guessing the agent's classification on every message?
4. Should the chip's collapsed line name the **predicate** (`Me · blood pressure`) or just
   the **count** (`Me · 4 written`)? The mocks show the former; it is the difference
   between a scannable margin and a noisy one when a note touches six entities.
