# F1 — Where note conversations live (three-mock GUI gate)

> **Status:** Research · **Last verified:** 2026-09-08

Design dossier for the **PROCESS.md GUI gate** on the agent-ingest change: *a note stops
being an input to a silent pipeline and becomes **turn 0 of an agent conversation***. The
agent reads the note, commits the graph changes it is confident about, asks about the rest,
and the owner's replies are how the entity/predicate database gets built and corrected.
Conversations are resumable indefinitely.

Three mocks were built, one per candidate home for that conversation. **Nothing is built in
`frontend/src`** — this dossier and the mocks are the whole deliverable.

**Mock files** (all in `docs/mocks/agent-ingest/`, with a family `README.md`):

| | File |
|---|---|
| **A — note view becomes the thread** | `docs/mocks/agent-ingest/a-note-thread.html` |
| **B — home stream *is* the conversation** | `docs/mocks/agent-ingest/b-stream-conversation.html` |
| **C — unified conversations surface** | `docs/mocks/agent-ingest/c-unified-conversations.html` |
| Family index | `docs/mocks/agent-ingest/README.md` |

All three are standalone, interactive, phone-viewport, dark **and** light, on the
`docs/reference/DESIGN.md` token sheet, and each stages the same scenario so they compare
directly: turn 0, a commit turn with the graph edges inline, an agent question with answer
affordances, the owner's reply, the resulting commit, a conversation still *thinking*, an
attachment inside a conversation, and the silent queue.

---

## 1 · The binding constraints

From the owner, and non-negotiable for every variant:

1. **Auto first pass on capture** — commit-confident, ask-unsure. The owner does not press
   "analyze".
2. **Scope** — the owner's own notes plus their attachments / OCR / media.
3. **Questions go into a silent queue.** No push notifications. No nagging badge.
4. **Phone, remote, no terminal** (`CLAUDE.md` #10).
5. **Local-only inference** — a first pass can take minutes, so a note's conversation may
   simply not be ready when the note is captured. Every surface must be able to say
   *"still working, nothing written yet"* without looking broken.
6. **The graph stays machine-written** (`CLAUDE.md` #7). An owner's answer is filed as a
   **correction**, never a hand-edited fact — the same channel the review inbox already
   uses (`docs/reference/DESIGN.md` "Review inbox", *Edit model*).

---

## 2 · What exists today (verified in `frontend/src`)

Every surface the change touches, read before proposing to replace it.

### The omnibox home — already half a conversation surface

`frontend/src/screens/HomeScreen.tsx:87` holds one `SegState`, and the **body of home is
mode-scoped**: `frontend/src/screens/HomeScreen.tsx:269-335` renders
`conversational ? <FullBrainSurface/> : <Stream/>` — the same screen, the same top bar, the
same docked composer, two different bodies. The `<Omnibox>` below
(`frontend/src/screens/HomeScreen.tsx:336-405`) routes a send to **either** `notes.send`
(capture) **or** `fb.send` (a conversation turn) depending on the mode
(`frontend/src/screens/HomeScreen.tsx:339-353`).

This is the single most important finding for this gate: **home is already a switchable
capture/conversation surface**, so none of the three options is architecturally exotic. What
differs is *how much of that switch the owner has to operate by hand*.

Note a doc/code divergence worth correcting whichever way this lands:
`docs/reference/DESIGN.md:677-684` still describes Research/Full Brain home as showing
"conversation cards" you tap into; the code has since made home render the **live
transcript** itself.

### The day-grouped transcript stream

`frontend/src/components/Stream.tsx:221-273` — bounded to `STREAM_DAYS = 2`
(`frontend/src/components/Stream.tsx:15`), grouped by day
(`frontend/src/components/Stream.tsx:235`), newest kept in view like a chat log
(`frontend/src/components/Stream.tsx:230-233`), with an "older notes live in Search ↑" pill
(`frontend/src/components/Stream.tsx:241-244`).

The **swipe action rail** is `frontend/src/components/Stream.tsx:108-156` (delete with a
tap-again arm, edit, hide), riding pure drag state in `frontend/src/notes/swipe.ts` with
`RAIL_WIDTH = 192` (`frontend/src/notes/swipe.ts:7`).

The row's status chips are `frontend/src/components/Stream.tsx:184-206`: attachment chips,
`pending sync`, and the derived **lifecycle chip**
(`frontend/src/components/Stream.tsx:29-33` → `frontend/src/notes/lifecycle.ts:40-54`).
Its documented doctrine matters here: *"analyzed → no chip (**the quiet end-state**)"*
(`frontend/src/notes/lifecycle.ts:8`).

### The Note / Analysis view

`frontend/src/screens/NoteScreen.tsx:297` — a three-way tab state
(`"note" | "attachments" | "analysis"`) **defaulting to `analysis`**; the tablist is
`frontend/src/screens/NoteScreen.tsx:388-420`, and the Attachments tab already carries a
**count pill** (`frontend/src/screens/NoteScreen.tsx:407`) — the precedent a `Thread` tab's
open-question count would reuse.

The Analysis tab is `frontend/src/components/AnalysisTab.tsx`: facts grouped by subject
(`frontend/src/components/AnalysisTab.tsx:50-70`), each rendered as a literal edge —
`edgePath(predicate, qualifier) → EdgeValue` with kind badge, status chip and confidence
(`frontend/src/components/AnalysisTab.tsx:78-115`) — and tapping a fact expands its
citation back to the highlighted source words
(`frontend/src/components/AnalysisTab.tsx:112`). **All three mocks reuse this idiom
verbatim** for "the graph change, rendered inline"; it is the repo's existing visual
grammar for a committed fact and nothing better needs inventing.

### Entity pages

`frontend/src/screens/EntityScreen.tsx:113-121` — the hub, with a per-predicate history
disclosure (`frontend/src/screens/EntityScreen.tsx:95`) that already carries superseded
values, and `EntityHistorySheet`. Closing a `lives_in` interval (the Priya case in all three
mocks) lands here untouched; **no option changes the entity pages**.

### The unified review inbox

`frontend/src/screens/ReviewScreen.tsx:1-30` — the split inbox (pending · decided lanes),
list → detail with prev/next, details **composed from a typed block registry**
(`frontend/src/review/blocks/registry`), grouped by subject entity
(`frontend/src/review/grouping.ts:37`), every decision undoable.

This is the surface the change is arguably *about*: today a low-confidence extraction
becomes a review card here. Under the proposal it becomes **a question in a conversation**
instead. Two things follow, and they hold for all three options:

- The inbox **does not go away**. Kinds with no single conversational owner — merge
  proposals, domain promotions, `wiki_contradiction` — have no note to be turn 0 of, and
  the launcher already surfaces the queue (`frontend/src/components/Launcher.tsx:90`).
- The inbox is also the **existing precedent for a non-nagging count**: its badge is
  rendered only inside the launcher (`frontend/src/components/Launcher.tsx:370-372`), polled
  only while the launcher is on screen (`frontend/src/components/Launcher.tsx:190`,
  `:194-196`). Nothing counts at the owner on home. Every silent-queue design below is held
  to that same bar.

### Full Brain chat + inline approvals

`frontend/src/agent/FullBrainSurface.tsx:301-357` is the transcript shell; `Bubble` is
`frontend/src/agent/FullBrainSurface.tsx:663`; a chat **attachment chip** inside a user
bubble is `frontend/src/agent/FullBrainSurface.tsx:419-450`; and a staged Proposal renders
**inline in the transcript** as `<InlineProposal>`
(`frontend/src/agent/FullBrainSurface.tsx:818`, component
`frontend/src/agent/InlineProposal.tsx`, kinds gated at
`frontend/src/agent/InlineProposal.tsx:14-24`). Transcripts are held per session
(`frontend/src/agent/useFullBrain.ts:367`) against an `AgentSession`
(`frontend/src/agent/types.ts:329-361`) that already carries `title`, `turn_count`,
`preview`, `staged_count`, `last_active_at` and a `parent_session_id` — i.e. **most of what
a note conversation needs already exists on the session object.**

The inline-approval doctrine (`docs/reference/DESIGN.md:1224-1240`) is the closest existing
relative of an agent question: agent proposes → owner approves/declines/corrects in place →
a single outcome returns to the agent so it follows up. The answer affordances in all three
mocks are deliberately drawn as its calmer sibling.

### Search

`frontend/src/screens/SearchScreen.tsx` — live as-you-type, passage-first cards, domain
filter chips (`frontend/src/screens/SearchScreen.tsx:21-27`). Search is where notes older
than two days live, and it is the natural long-tail path back to an old conversation. No
option needs to change it; **B** and **C** both make it *less* load-bearing.

---

## 3 · The three options

### A — the note view becomes the thread

`docs/mocks/agent-ingest/a-note-thread.html`

The `Analysis` tab becomes `Thread`. Turn 0 is the note (rendered as a source-of-truth card,
not a chat bubble — it *is* the source of truth); below it the agent's commit turn with the
edges inline, its questions with tap-to-answer chips, the owner's replies, and the commits
those produce. Home, the omnibox and Full Brain are **untouched**.

Silent queue: the row's **lifecycle chip** gains a terminal state — `3 committed · 1 open` —
plus one scroll-away *"one note here is still asking about something"* line per day card.

The mock also shows the slow case: a second note whose first pass is four minutes in, with
`still working through it · nothing is written yet` and its OCR already quoted.

### B — the home stream *is* the conversation

`docs/mocks/agent-ingest/b-stream-conversation.html`

Capture and conversation become one motion. The note row is unchanged — same clamp, same
domain dot, same swipe rail — and the agent's turns **hang off it on a hairline spine**, so
the transcript interleaves the owner's notes and the agent's turns without ever losing which
note is being discussed. While a question is open the Entry composer reads **replying to
"…"**; a send goes into that note's thread, and ✕ drops the pill so the very same box
captures a brand-new note again.

Silent queue: the Entry footer's **existing microcopy line** carries the count
(*"Saved to your wiki · 2 still asking"*) and tapping it **filters the stream in place** to
notes with an open question, oldest first. A `1 older, unanswered` rule sits in the day
header as an in-list jump, never as chrome.

### C — a unified conversations surface

`docs/mocks/agent-ingest/c-unified-conversations.html`

A note thread and a Full Brain chat stop being two things: both are an `AgentSession`. Home
becomes the **conversations list** — the already-settled chats picker (segmented buckets
with count pills, ~46px micro rows, scope dot, 4-action swipe rail, live-turn activity
glyph) with one bucket added: **`Today · Older · Asking`**. Opening a note conversation lands
on the Full Brain transcript with turn 0 = the note. Because the object is the same, the
thread does not dead-end — the mock shows the owner asking *"what else do I have on her?"*
and getting a full-retrieval answer with an entity pill, **in the same thread**.

Silent queue: the **`Asking` bucket** is the whole queue. Its count exists only on a segment
the owner chose to look at; nothing counts on home chrome or in the launcher.

*(The brief invited substituting a stronger third option. I kept C: it is the only one of
the three that answers "what happens when the owner wants to keep talking about a note",
and the repo has already settled every component it needs.)*

---

## 4 · Comparison

### Phone ergonomics

| | Verdict |
|---|---|
| **A** | **Weakest.** Answering costs a navigation per note: home → tap row → (tab already defaults to the analysis slot, so at least it lands right) → answer → back. Two notes with questions is two round trips. The thumb is fine once you are in the thread; getting there is the cost. |
| **B** | **Strongest for the common case.** Everything happens in one scroll with the composer already under the thumb, and there is no navigation at all. The cost is **vertical**: a stream bounded to two days already fills a phone, and hanging three or four agent turns off each note can push the newest capture off-screen — the very thing the "newest at the bottom, keep it in view" rule exists to protect (`frontend/src/components/Stream.tsx:230-233`). Collapsing settled spines is mandatory, not optional. |
| **C** | **Strong and predictable.** ~46px rows are the densest thing in the repo, and the picker's own review settled that density deliberately. Cost: capture is now *always* one tap from a list rather than a box you type into — the mock keeps the omnibox docked to soften that, but home is no longer "the stream I glance at". |

### Discoverability of unanswered questions, with no push and no nagging badge

| | Mechanism | Honest assessment |
|---|---|---|
| **A** | Per-row chip + a scroll-away sentence per day card | **Only as good as the two-day window.** A question on a note that scrolled past two days is discoverable only by remembering the note and finding it in Search. It also **breaks the lifecycle chip's stated doctrine** — *"analyzed → no chip (the quiet end-state)"* (`frontend/src/notes/lifecycle.ts:8`): the calm end-state becomes a permanent amber chip on every row that ever asked something. That is a badge, and on the surface the owner looks at most. |
| **B** | Footer count + in-place stream filter + an in-list jump rule | **Good, and honest about staleness** (`asked 6 days ago · no rush`). The count sits in the composer footer, which is chrome the owner reads constantly — calmer than a red dot, but it *is* a persistent number in the one place the eye always lands. Whether that crosses the "nagging" line is a genuine owner call, and it is the single question I would put first. |
| **C** | The `Asking` segment | **Cleanest reading of the constraint.** The count exists nowhere until the owner opens the picker and chooses that segment — exactly the Review-tile precedent (`frontend/src/components/Launcher.tsx:370-372`), one level closer to hand. Oldest-first ordering makes the queue self-draining. Risk is the mirror image: a bucket you never tap is a bucket you never see, so questions can age indefinitely. That may be precisely what the owner asked for. |

### Frontend reuse vs. replacement

| | Reuses | Replaces / net-new |
|---|---|---|
| **A** | Everything on home; the whole `Stream`; the whole Full Brain stack; the edge/citation rendering (`frontend/src/components/AnalysisTab.tsx:78-115`); the tab shell + count pill (`frontend/src/screens/NoteScreen.tsx:388-420`). | One tab body, plus a question-card component and a per-note transcript store. **The smallest diff of the three, by a wide margin** — one screen, no home changes, no navigation changes. Also: the note view is a *layer*, so the Full Brain composer's lateral swipes and the back-gesture stack (`docs/reference/DESIGN.md:1240-1270`) are untouched. |
| **B** | The `Stream` rows and rail; the outbox; the omnibox shell. | **`Stream.tsx` is rewritten** — its item model becomes note + turns, the day card becomes a unit list, and the rail must not fight the new content. `Omnibox` gains a reply-target mode that changes what a send *means* (`frontend/src/screens/HomeScreen.tsx:339-353`) — the riskiest edit in the whole change, because that is the capture path. Two transcript renderers now exist (the spine and `FullBrainSurface`) and will drift. |
| **C** | The most, structurally: `AgentSession` (`frontend/src/agent/types.ts:329-361`), `useFullBrain`'s per-session transcripts (`frontend/src/agent/useFullBrain.ts:367`), `FullBrainSurface` + `Bubble` + `AttachmentChip` + `InlineProposal`, the settled chats picker and its 4-action rail, the live-turn glyph. **One transcript renderer for everything.** | **Home is replaced** — `Stream.tsx` stops being the home body, and with it the day-grouped note stream as a concept. Backend-side this is the largest ask: a note must *become* a session (or gain one), and every place that assumes "notes list" vs "sessions list" is touched. |

### Offline behaviour (the IndexedDB outbox)

Today's outbox is `frontend/src/notes/outbox.ts`: an IndexedDB store (`jbrain` / `outbox`,
`frontend/src/notes/outbox.ts:53-69`) holding `PendingNote` rows **including attachment
Blobs** (`frontend/src/notes/outbox.ts:16-32`), flushed on send, on reconnect, while the
stream is visible, and by a retry while anything is pending
(`frontend/src/notes/useNotes.ts:151-184`). `POST /api/notes` is idempotent on `client_id`.
A queued row renders with a `pending sync` chip and **`item.id === null`, which disables the
swipe rail entirely** (`frontend/src/components/Stream.tsx:63`).

Agent turns are the opposite: `useFullBrain` streams over SSE and keeps transcripts **in
memory**, with no outbox at all. So the real question is *what an answer typed offline
does*.

| | Offline story |
|---|---|
| **A** | **Cleanest separation.** Capture stays exactly as it is. An offline note simply has no thread yet — the tab shows "not captured on the box yet", which is true and easy to render. Answers need their own small outbox (an answer is a correction, so it can ride the existing note-shaped queue: file it as a correction note, `provenance=owner_correction`, and the pipeline applies it on flush). |
| **B** | **Most exposed.** The composer now has two meanings and offline changes them differently: a capture queues locally and appears immediately (today's behaviour), an answer *cannot* be applied until the box has the thread. Rendering a pending answer inside a spine whose note is itself still pending is a genuinely awkward state, and it lands on the capture path — the one flow that must never get slower or less trustworthy. |
| **C** | **Middle.** An offline capture must create a *conversation* row optimistically, so the list needs a local, id-less session — new machinery `useFullBrain` does not have. Once built it is uniform (one queue, one row type), but it is the largest offline change, and it puts the outbox under the surface the owner uses to capture. |

### Risk

| | Principal risk |
|---|---|
| **A** | **Under-delivers the vision.** The conversation exists but is filed away one navigation deep, so the back-and-forth that is supposed to *build the database* competes with everything else on the phone. It also makes the note view carry two jobs (archive + live thread) and quietly turns the lifecycle chip into a permanent badge. Low build risk, real product risk. |
| **B** | **Puts the capture path at risk.** Capture is the app's most-used, most-trusted flow; B rewrites the stream it renders into and overloads the composer that drives it. Also the highest chance of a stream that becomes unreadable in a busy week, and the only option that creates a **second** transcript renderer to keep in sync with `FullBrainSurface`. |
| **C** | **Biggest surface-area change, and the most to get wrong at the seam.** Unifying notes and chats into one object means the domain firewall now has to hold across a boundary it did not previously span: a note conversation is scoped to its note's domain, a Full Brain chat is scoped by the session's `domain_scopes` (`frontend/src/agent/types.ts:335`), and "keep talking in the same thread" must not let a `health` note's conversation widen into a general-scope read. That is an RLS/firewall question (`CLAUDE.md` #3) and it deserves a red-team pass before any code. Losing the glanceable day-grouped stream is a real, if smaller, cost. |

---

## 5 · Recommendation

**Build C — the unified conversations surface — and stage it so B's ergonomics are still
reachable.**

The reasoning, in order of weight:

1. **It is the only option whose silent queue actually satisfies the constraint as
   written.** The owner asked for a queue with no push and no nagging badge. C's `Asking`
   segment is a count that does not exist until you go looking, which is the same shape as
   the Review tile badge the repo already settled. A's chip and B's footer number both put a
   standing count on a surface the owner reads all day.
2. **It reuses the most and forks the least.** One transcript renderer, one session object,
   one picker. A and B both end with two ways to draw a conversation — and the second one,
   in both cases, is the one that has to render tool views, citations, attachments and
   staged proposals *eventually*, at which point it converges on `FullBrainSurface` anyway.
   C starts where the others end up.
3. **It is the only option that lets the conversation keep going.** The premise is that the
   back-and-forth *is* how the database gets built. In A and B, a note thread is a
   fixed-scope Q&A about one note; in C the owner can follow a thought — *"what else do I
   have on her?"* — without leaving, because the thing they are in is already the Full Brain
   surface.
4. **The change is architecturally cheaper than it looks**, because home already switches
   between a capture body and a conversation body on one segment
   (`frontend/src/screens/HomeScreen.tsx:269-335`). C changes *what the capture body is*, not
   how the screen is built.

Two conditions on that recommendation:

- **The firewall question is settled first.** Before any code: does a note conversation
  inherit its note's domain as a hard scope, and what happens when the owner asks a general
  question inside a `health` note's thread? Fail-closed (the thread keeps the note's scope,
  and a broader question offers to open a new chat) is the answer I would default to, but it
  needs a red-team pass, an RLS isolation test, and a line in `docs/reference/ASSISTANT.md`.
- **B's one genuinely better idea is stolen.** B's *reply-to pill on the composer* — where
  answering is the box you already have, not a navigation — is the best ergonomic idea in
  the round. It transplants cleanly into C: with an `Asking` row open, the composer carries
  the same pill. If the owner wants to keep the day-grouped note stream on home, **A+B's
  stream can be preserved as a bucket** rather than a rival surface — but that is a second
  round, not this gate.

If the owner's priority is **minimum disruption to a system they are living in daily**,
A is the honest choice and I would not argue hard against it — it is one screen, it ships
fast, and it can be superseded by C later without wasted work, because the question card and
the edge-commit card are the same components either way. What I would not recommend is B:
it takes the largest risk (the capture path) for a benefit (no navigation) that C can borrow.

---

## 6 · Open questions for the owner

1. **Is a persistent count in the composer footer "nagging"?** B's whole discoverability
   story depends on the answer, and so does whether C's `Asking` count may ever be echoed
   outside the picker. This is the question that decides the round.
2. **Should an unanswered question ever escalate?** After a month of silence, does it stay
   silent forever, quietly become a review-inbox card (where it *is* visible in the launcher
   badge), or get dropped with the agent committing its best guess at low confidence?
3. **Does a note conversation inherit its note's domain as a hard scope?** And may the owner
   widen it in-thread, or must a broader question start a new chat? (Firewall / RLS —
   `CLAUDE.md` #3.)
4. **What happens to the review inbox?** Do low-confidence extractions stop producing cards
   entirely and become questions instead, or do both exist — cards for kinds with no single
   note owner (merges, domain promotions, `wiki_contradiction`), questions for everything
   else?
5. **Can the agent ask about more than one note at once?** A week of gym notes probably
   deserves *one* question, not seven. C makes that natural (a conversation spanning several
   notes); A cannot express it at all, since a thread belongs to exactly one note.
6. **May an answer be spoken?** Read-aloud already exists on the conversation surface; a
   voice answer to a queued question is the most phone-native version of this whole idea, and
   it changes which option wins if it is in scope.
7. **What does the owner want to see when the box is slow?** All three mocks show
   `still working through it · on-box · 4 min in · nothing written yet`. Is that reassuring,
   or would a bare absence be calmer?
8. **If C: does the day-grouped note stream survive** as a bucket in the picker, or is it
   retired? It is the surface the owner has used longest, and retiring it is the largest
   felt change in the whole proposal.
