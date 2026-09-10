# The note's own thread — how a batched ask is answered

> **Status:** **Settled, single mock.** `note-thread.html` is the binding spec for the note
> conversation's interaction, walked end to end with the owner and ratified there. Not a
> three-variant round: it was built to answer questions the prose in
> `docs/plans/AGENT_INGEST_REWRITE.md` had left open, and answering them settled them.
> The spec written out of it is that plan's **§3b**, and the build is its **R3f** wave
> (backed by **R1c**, the batched ask). Companion gate:
> `docs/mocks/agent-ingest/README.md` (where a note thread lives — variant C, D1).
> **Last verified:** 2026-09-10.

## What it settles

1. **Every interaction about a note happens inside that note's conversation.** The stream
   row carries a chip and no verb — a redirect, the same ruling `NotesInboxEntry` already
   enforces on the wire.
2. **The transcript is the normal agent paradigm** — the violet Thought chip and the steel
   Worked chip, the live phase line, the step rows — not a bespoke ingest view.
3. **The question block is inert.** Selecting a candidate or typing an answer is local
   state; it cannot start a turn.
4. **The omnibox send is the only submit.** One send is one user turn carrying every
   answer plus any typed text. Three answers cost one turn, not three.
5. It is the artefact that decided **O9** (batched asks) and opened **O11** (partial send)
   and **O12** (draft state).

## Read it against the code, not instead of it

The mock is a **proposal**. It is built on the real token sheet and deliberately mirrors
the shipped transcript classes (`.fb-act-think` / `.fb-act-work` / `.fb-step-row` /
`.fb-think-tool` in `frontend/src/styles.css`), but it is not a description of the PWA.
§3b of the plan names every place the two disagree and says which wins — the mock overrules
the code on the question block and the carry strip; the code overrules the mock on the
Thought/Worked panel (one body, not two), the live phase line's home, the settled row's
chip, and the mode row, which the mock hides and the app must not.

Its own rail names the two gaps it exposed: the ingest verbs missing from `status.ts`'s
live-phase label map, and a half-filled block with nowhere to live.
