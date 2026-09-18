# Ask the user — a tool that stops and asks instead of guessing

> **Status:** GUI gate **open** — three variants, awaiting the owner's choice. Nothing
> built, no `ask_user` tool exists yet.

## The problem

The agent has no way to ask a question and *wait*. It can stage a Proposal and it can end
a turn, but there is no path for "I need one fact from you before the rest of this is
worth doing." So it guesses, and a guess that reads fluently is indistinguishable from an
answer — the same failure class the arithmetic backstop was built for, one level up.

## What already exists, and why this is not it

**The synchronous half already ships.** `InlineProposal` (from the settled
`../inline-approvals/` gate, variant D) is an interactive card in the transcript that
takes an owner action and **sends a message back to the assistant so it can follow up**.
`ask_user` is that exact mechanism with a question in place of a diff. No new plumbing —
the turn-ending-and-resume path is the one `DeferredRef` and the proposal outcome already
use.

**The asynchronous half is designed and ratified but unbuilt.**
`../../plans/AGENT_INGEST_CONVERSATION_PLAN.md` — *"awaiting owner sign-off… No code
written yet"* — already decides how this system asks questions:

- **D2:** questions expire on a ladder (7d long-tail, 30d registry predicates, 60d merges)
  and **decay to a marked assumption**. **Never** for health, finance or location.
- **D7:** an owner's answer **mints an owner-authored note**, so the graph stays
  re-derivable.
- **D8:** GUI variant C — the queue lives in the launcher, **no nagging badge**.

**The risk this gate exists to avoid is two incompatible question systems.** An `ask_user`
that invents its own lifecycle, its own expiry and its own store would collide with that
plan the moment it is built. Every variant below is drawn so the two are the *same*
question with two urgencies.

## Built on the shipped stylesheet

All three inline the `:root` tokens and every `.bubble` / `.fb-inline-prop` / `.ip-*` /
`.fb-composer` rule verbatim from `frontend/src/styles.css`, with only the `.fb-shell`
scope prefix dropped. Each mock's stylesheet is split into three labelled blocks — shipped
CSS, phone-frame scaffolding, and that variant's proposed additions — so a reviewer can see
exactly what is being asked for.

That is not decoration: it is what makes the "no new plumbing" claim checkable. **A and C
are `InlineProposal`'s own frame**, down to `.ip-head`, `.ip-tree`, `.ip-leaf`, `.ip-foot`,
the `.ip-enact.armed` double-tap state and the `.fb-inline-prop.done` settled state. The
additions are a question line, a `why`, and an option row's selected state — the shipped
leaf carries approve/decline controls where a question carries a choice.

**C's park state needed no new colour either.** `.ip-held` / `.ip-held-badge` already exist,
already amber, for a leaf whose prerequisite is declined and which therefore reads *held*
(DESIGN.md). A parked question is that state exactly, so the assumption block borrows the
vocabulary rather than inventing an amber of its own — which is a small sign that the
park-don't-block idea fits the system rather than being bolted to it.

## The three

| | Shape | Best at | Worst at |
|---|---|---|---|
| **A** `a-inline-card.html` | Question card in the transcript; tappable suggested answers; double-tap to send | Question sits with the reasoning that raised it; common case is one tap; matches the shipped `InlineProposal` doctrine exactly | The turn visibly stops; a chatty model turns the transcript into a form |
| **B** `b-composer-pill.html` | No card at all — question is prose, the **composer** changes (pill + answer chips) | Lightest possible surface: no new registered view, answer where your thumb already is | The question scrolls away; nothing anchors *what* is waited on in a long chat; no record once answered; a second question has nowhere to go |
| **C** `c-answer-or-park.html` | A, plus **what it will assume if you say nothing**, plus **Park it** | The question is never a *block*; the only variant wired to D2's ladder, its decay-to-assumption, and its `never` for health/finance/location | Tallest card; an assumption line to read on every question |

## The judgement

**C, and not narrowly.**

The stated goal is *"it stops the model from guessing when it shouldn't."* A and B deliver
that by **blocking** — and a blocking question is only an improvement while you are
willing to answer it. The moment you are not, a blocking question is worse than a guess,
because the work stops entirely. C is the only variant where "I don't want to answer this
right now" is a first-class outcome rather than an abandoned turn.

C also carries the thing that makes a guess safe when one is unavoidable: **it says what
it will assume, before you decide.** That converts an invisible guess into a stated one —
which is the entire move, and the same move the arithmetic tools made.

And C is the only variant that will still be correct after the ingest plan is built,
because it is the ingest plan's own model (ladder, decay, marked assumption, domain
`never`) rendered synchronously.

**A is the honourable fallback** if C's card reads too heavy in practice — it is A plus
two rows, so C can degrade to A without rework. **B should not be chosen**: its cost is
not aesthetic. A question with no transcript record is a question you cannot audit, and
this tool's whole premise is making the model's uncertainty visible.

## What the gate does not settle

These are implementation questions for the build plan, flagged now so the chosen mock is
read with them in view:

- **Whether `ask_user` can be abused by injected content.** The question text is
  model-authored, and a note body or web page could induce a plausible-looking question
  ("confirm your account to continue"). It renders as *text in a first-party card*, which
  is what tool results already do — but the card must never render a link, a field that
  looks like a credential prompt, or anything but the closed set of affordances mocked
  here. Worth a red-team pass before it ships.
- **Which personas hold it.** `intake` must not — a non-owner stranger's session must
  never be able to put a question in front of the owner.
- **Whether a parked question is the same row as an ingest question.** It should be. One
  store, one expiry ladder, one queue.
