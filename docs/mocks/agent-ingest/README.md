# Note conversations — where a note's agent thread lives

> **Status:** GUI gate **settled on C**, `c-unified-conversations.html` — but read how.
> Choosing C was a **delegated** call made inside this round, **not an owner gate
> outcome**, and no ratified decision names a variant letter. What the owner ratified is
> `docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md` **D1** — *"one agent, one conversation
> type: a note conversation is the same agent, loop and memory as chat"*, which in the
> owner's own words at the gate meant a note conversation and a chat should be literally
> the same thing, and **not a hidden path**. D1 ratifies C *in substance*: one
> `AgentSession`, reached from the conversations surface rather than the note screen.
> **Cite D1 for that, never "the owner chose C".** A and B are retained as the record.
> **One part of C is superseded**: its `Asking` bucket is replaced by the **two-tab
> inbox** (**D4**), with questions findable from the notes tab (**D5**) — so the mock's
> segmented `Today · Older · Asking` picker is no longer the queue answer.
> Companion dossier: `docs/research/agent-ingest/F1-GUI-MOCKS.md`. Follow-on round (what the *note screen*
> becomes under this model): `docs/mocks/agent-ingest-note-body/`. Nothing is built.
> **Last verified:** 2026-09-08.

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
