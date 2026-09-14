# Note conversations — where a note's agent thread lives

> **Status:** **BUILT — Entry hosts the conversation**, after the owner **reversed this
> gate twice on 2026-09-14**: off the conversations surface (variant A won), and then off
> the note screen as well. The final shape is in neither mock: see *Where it ended up*
> below.
>
> This round originally settled on **C** (`c-unified-conversations.html`) — a *delegated*
> call made inside the round, not an owner gate outcome. What the owner ratified was
> `docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md` **D1** — *"one agent, one conversation
> type: a note conversation is the same agent, loop and memory as chat"* — and this README
> read D1 as also settling **where he reaches it**: *"one `AgentSession`, reached from the
> conversations surface rather than the note screen"*. **That second half is now
> reversed.** Having used the shipped result (a note screen with an *Add a thought* button
> that handed off to home's Full Brain chat), he said:
>
> > *"This is not matching my mockup at all where I wanted this to basically be another
> > agent conversation. I can't see either thinking or anything else. It just has me to add
> > on the conversation with another button. **When I click on the note, it should
> > basically open up as a normal agent conversation same as jerv.** When I go to do a
> > follow-up, **it shouldn't open in the brain chat. It should open up right there in the
> > note entry chat.** I thought we had even mocked this so I'm a little confused"*
>
> — and, asked whether to build it properly: *"Yeah we need to build it properly."*
>
> **⟲⟲ Where it ended up.** Variant A was built — the note screen's tab row became
> **Thread · Note · Files**, opening on Thread, with a composer of its own on the tab — and
> the owner rejected THAT the same day, with a screenshot:
>
> > *"This is still not presenting right? **You should use the same omnibox as everything
> > else**, but **the conversation of the main view should change to the note** and then
> > have **the ability to go back to the note list by hitting back on the top left**."*
>
> Asked where the attachments and facts should then live, he reframed it instead of
> choosing, and this is the settled spec:
>
> > *"**I want you to keep the one omnibox just like jerv. The difference is the default
> > view of entry would be notes. And when you select a note, it basically loads a
> > conversation the same as if I had swiped left inside of jerv and picked a different
> > conversation.**"*
>
> So **Entry is the host**, not the note screen. The notes list is Entry's session picker;
> selecting a note loads its thread into the main view; the composer is the app's ONE
> omnibox, mode row intact; back at the top left returns to the list. The note screen keeps
> the record — **Note · Files**, the body, the eraser, the facts and their re-run controls —
> one tap away, on the conversation's top bar. Neither A nor C draws this; `DESIGN.md`
> ("The omnibox home" → *Entry is a conversation surface too*) is the spec of record.
>
> **What is NOT reversed — D1's substance.** A note conversation is still one
> `AgentSession` with the same agent, loop and memory as chat, and Entry renders the
> *shipped* transcript component (`frontend/src/agent/FullBrainSurface.tsx`'s
> `AgentTranscript`), not a second ingest rendering. What DID change is which tab lists it:
> `note_ingest` is an **Entry** conversation now, off the Full Brain Chats panel
> (`useFullBrain.MODE_AGENTS`), because Entry's notes list is its picker and the same chat
> behind two pickers is the confusion this round kept producing. C's `Asking` bucket remains
> superseded by the **two-tab inbox** (**D4**) with questions findable from the notes tab
> (**D5**).
>
> **B is retained as the record**, as is C — read C for the one-conversation-type argument
> D1 ratified, and A for the transcript treatment that was built (its tab row was not). The follow-on round that asked what
> the *note screen* becomes (`docs/mocks/agent-ingest-note-body/`) was scrapped before its
> gate on the premise that *"the note screen does not change"*; that premise is now false,
> and its `SUPERSEDED.md` records the reversal. Companion dossier:
> `docs/research/agent-ingest/F1-GUI-MOCKS.md`. Interaction spec inside the thread:
> `docs/mocks/agent-ingest-thread/README.md`.
> **Last verified:** 2026-09-14.

## The change these mocks are for

Today a note is captured and a **background pipeline** silently extracts facts, spilling
review cards into the unified inbox. The proposal is that **a note becomes turn 0 of an
agent conversation**: the agent reads it, commits what it's confident about, asks about the
rest, and the owner's replies are how the entity/predicate graph gets built and corrected.
Conversations are resumable indefinitely.

Owner decisions already binding on all three: **auto first pass on capture**
(commit-confident / ask-unsure); scope is owner notes plus attachments/OCR/media;
**questions go into a silent queue — no push notifications, no nagging badges**; the owner
is on a phone, remote, no terminal; inference is **local-only**, so a first pass may take
minutes and a note's conversation may not be ready the moment it is captured.

## The three variants

| | File | Where the conversation lives | How an unanswered question is found later |
|---|---|---|---|
| **A** | `a-note-thread.html` | The **note view** becomes the thread — its `Analysis` tab is replaced by `Thread`. Home stream, omnibox and Full Brain are untouched. | The **note row's own lifecycle chip** gains a terminal state (`3 committed · 1 open`) plus one scroll-away "still asking about something" sentence per day card. |
| **B** | `b-stream-conversation.html` | The **home stream itself** — agent turns hang off each note on a hairline spine; the Entry composer becomes the reply box while a question is open, and drops back to capture with ✕. | The Entry footer's **existing microcopy line** ("Saved to your wiki · no AI.") carries the count and **filters the stream in place**; a `1 older, unanswered` rule sits in the day header. |
| **C** | `c-unified-conversations.html` | A **unified conversations surface** — a note thread and a Full Brain chat are the same `AgentSession`, so home becomes the conversations list and the thread is the Full Brain transcript with turn 0 = the note. | A third bucket on the settled chats-picker segmented control: **`Today · Older · Asking`**. The count exists only on that segment, inside that surface. |

Each mock stages the same scenario end to end so they are directly comparable: turn 0 (a
note about Priya), an agent turn that **says what it committed** with the graph edges
rendered inline (including a closed `lives_in` interval), an **agent question** with
tap-to-answer affordances, the **owner's reply**, the **resulting commit** rendered inline,
a second conversation that is **still thinking** (on-box model, four minutes in, nothing
written), a **photo inside the conversation** (thumbnail + OCR quoted by the agent), and
the **silent queue**.

All three are standalone, interactive, phone-viewport, dark **and** light (the toggle in the
demo bar), and built on the `docs/reference/DESIGN.md` token sheet — no raw hex outside the
token block, ≥44px targets, `prefers-reduced-motion` honored. Colour stays informational:
green = committed/saved, amber = open question/pending, steel = agent/info, rose = medical,
violet = financial.

## Where Analysis went

A's own caption says only *"its `Analysis` tab is replaced by `Thread`"*, and its tab row
reads **Thread · Note · Files** — so the round drew the replacement without saying where
Analysis's content goes. It goes into **Note**, whole, under a `What this note says` rule
(`frontend/src/screens/NoteScreen.tsx`) — and stayed there when the `Thread` tab itself was
deleted that evening and the conversation moved to Entry, so the note screen's tabs are now
just **Note · Files**. Dropping the record was never an option and the choice is deliberate:

- The transcript is a record of **decisions**; the fact table is a readout of the **current
  head**. A value superseded a month later still reads *written* in the turn that wrote it,
  and only the table says what is true now.
- It carries the only no-terminal **re-run** controls the box has (note-level and per-image)
  plus the OCR / audio-transcript expansions of the Sources card — none of which a turn can
  host, and all of which CLAUDE.md #10 says must stay PWA-operable.
- A separate tab for it was rejected: *"the note, and what it says"* is one reading.
  `Attachments` is renamed **Files** to match the mock; it keeps the manifest and the
  per-file ⋯ sheet exactly.

The **home stream is not quite untouched** either, contrary to A's caption: the ask chip and
the row both SELECT THE NOTE, loading its conversation into Entry's main view, because two
doors to one place is the confusion this round created.

## What they reuse

- The **edge rendering** is the Analysis tab's own idiom (`frontend/src/components/AnalysisTab.tsx:78`)
  — `subject.predicate → value`, a confidence figure, and a tap that expands the citation
  back to the highlighted source words.
- The **question card** borrows the inline-approval doctrine
  (`docs/mocks/inline-approvals/d-one-tree.html`): the agent proposes, the owner disposes,
  and the owner's edit is filed as a **correction**, never a hand-written fact
  (non-negotiable #7).
- **C** reuses the settled chats picker verbatim — segmented buckets with count pills,
  ~46px micro rows, the scope dot, the 4-action swipe rail, and the live-turn activity
  glyph (`docs/mocks/session-picker/c-segmented-micro.html`,
  `docs/mocks/session-active-turn-glyph.html`).
- **A** and **B** keep the home stream exactly as `frontend/src/components/Stream.tsx`
  draws it — day cards, 3-line clamp, domain dot, swipe rail.

## Open questions for the owner

See the dossier's closing section — it carries the full list, including whether an agent
question may ever *become* a review-inbox card, and whether answering by voice is in scope.
