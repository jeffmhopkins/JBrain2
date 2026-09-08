# The note screen under the conversation model — frozen body, clarifications, write chips

> **Status:** GUI gate **OPEN** — three variants for the owner to choose from. Nothing is
> built. **Last verified:** 2026-09-08. Plan: `docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md`
> (D1, D3, D6, D7, D12; constraints 1, 4 and 6). Tool semantics:
> `docs/research/agent-ingest/TOOL_SURFACE.md`.

## What is already settled, and therefore not on offer here

**The conversation does not live on the note screen.** The companion round
(`docs/mocks/agent-ingest/`) chose **C — unified conversations**, and the plan ratifies it
as **D1, "one agent, one conversation type"**: a note thread and a Full Brain chat are the
same `AgentSession`, reached from the conversations surface. (That round's README still
said "gate open" — stale, since ratification; **fixed in this branch**. Its `Asking`
bucket is separately superseded by the two-tab inbox, D4/D5.)

So all three variants below put the transcript in the same place — behind one identical
**`open the conversation · N turns →`** row — and none of them re-opens that question. A
variant that made the Note tab a chat, or gave it a `Thread` tab, would be re-litigating a
ratified decision and would leave the owner one real choice dressed as three.

What *is* open, and what these three genuinely fork on:

1. **How a clarification reads inside the note** — prose, a version, or a log.
2. **Where the writes are surfaced** — anchored in the text, collected in a strip, or
   collected in a tab.
3. **Whether the tab bar changes at all** (see the table).

## The change these mocks are for

**1 · The body freezes and grows an appendix (D6/D7).** A note keeps the body you
captured, immutable, and gains **timestamped clarification blocks** as you answer the
agent's questions — minted by the engine on every owner turn, not by a tool. They are
chunks of the same note, so the graph re-derives from notes alone and every citation has a
real chunk (D7). That makes them *note text*, not chat history, and the reader must be
able to tell **what they wrote at capture** from **what they added later**. Today
`frontend/src/screens/NoteScreen.tsx:79-84,421-425` renders the body as one
undifferentiated Markdown blob.

**2 · The "entity modified" chip (D3).** Every graph write is visible as a chip you expand.
There is no approve button — a clear fact **commits** (D2), and disagreement is a reply —
so the chip's whole job is legibility. **The same six-state fixture is rendered in all
three mocks** (frame 2 of each), so nothing is chosen on the strength of a richer example:

| state | what the expanded chip must show |
|---|---|
| **written** | `subject.predicate → value`, when, the highlighted source words, and **the domain it landed in, in words** |
| **replaced** | the before→after, and that the old value is **closed, not deleted** — it keeps its interval on the entity's history rail |
| **held** | the clash, and *recorded but not live*. `held` is what `decide()` itself cannot settle (a conflict, or a pinned head) — never a confidence gate — and it is also how a **preliminary lab reading** waits for its final report (constraint 4) |
| **from a photo** (D12) | that it came from an attachment and **not from your note text**, with the verbatim OCR it was read off |
| **failed** | the error text the tool actually returns, auto-opened — the settled `StepRow` behaviour |
| **truncated** | a pass that hit its step limit **wrote what it wrote and swept nothing** (constraint 6) |
| **writing…** | the in-flight stages: appended → re-chunked/re-embedded → re-reading the note → N articles to rebuild |

**The write verb matters and the copy says so.** A fact minted by answering a question is
an ordinary **`assert_fact`** sourced to the clarification block. It is **not** a
correction and **nothing is pinned**: `correction` is deliberately withheld from
`assert_fact` and reserved for `correct_fact`, the on-reply verb for *the owner
disagrees*, which force-supersedes **and pins** (`TOOL_SURFACE.md`). Pinning every
answered clarification would be actively harmful — pinned facts are exactly what the
settle sweep must never retract.

**Domain is stated in words, never carried by a dot.** The fixture note is captured in
**general**, and its clinical values **ratchet up into health** — the case constraint 1's
derived same-domain citation chunk exists for — while `okonjo.specialty` stays **general**.
Each chip says which domain the write landed in and that the floor, not the model, decided
it: the agent has no `domain` field, only a strictly-upward `sensitive` dial.

**The wait is shown, not hidden.** Plan risk 4: an answered question costs a full
re-chunk, re-embed, re-integration and one article rebuild per mentioned entity, on a
serial GPU, while you wait. Answering in any of the three mocks puts the new chip through
its real in-flight stages before it settles.

## What they reuse (and one thing they deliberately don't)

- The chip is the settled **domain-dotted entity chip** + the **`StepRow` disclosure**
  (`frontend/src/agent/FullBrainSurface.tsx:1706-1746`, `:1752-1855`) — tap the row,
  detail in place, a failed step opens itself. Labels follow
  `frontend/src/agent/toolSummary.ts`.
- Edge rows are the Analysis-tab / entity-page idiom: monospace
  `subject.predicate → value`, expanding to the **highlighted source words**.
- OCR is quoted the way the settled **Sources card** quotes it — a quiet monospace inset.
- The question card follows the inline-approval doctrine
  (`docs/mocks/inline-approvals/d-one-tree.html`): the agent proposes, the owner disposes.
- **The before→after block is *adapted* from `frontend/src/review/blocks/ClaimDiff.tsx`,
  not reused verbatim** — same two-rows-and-an-arrow shape, **relabelled `was` / `now`**.
  Shipping it verbatim would put the word *proposed* on a fact that is already committed,
  which is precisely the approval implication this design removes. Whichever variant wins,
  that relabel is part of the spec.

Standalone, no build step, phone-viewport first, **light and dark** (they open in the
viewer's `prefers-color-scheme`), tokens only — no raw hex outside the token block,
`:focus-visible` rings, **≥44px on every interactive row inside the phone frames** (the
demo bar's theme and text-size toggles are mock chrome, not app UI),
`prefers-reduced-motion` honored including the scroll. They are drawn at **`--font-scale: 0.75`, the app's real default**,
with a 100% toggle in the demo bar for comparison.

**The fixture is identical in all three:** a note captured in `general` — cardiology
follow-up, a photo of the new prescription — producing `me.blood_pressure → 128/82 mmHg`
(written, ratcheted to health), `me.medication` **amlodipine 10 mg → losartan 50 mg**
(replaced, old value closed), `me.weight → 178 lb` (**held**, clashes with 182 lb from the
home scale), a new `Losartan` entity, `quantity → 90 tablets` and `refills → 3` read
**only off the label**, `okonjo.specialty → cardiology` (**failed** on a bad handle, then
written into **general**), a **truncated** first pass, one answered clarification (08:05,
the ankle), and one open `ask_owner` question — *"bloods again" — which panel?* — that you
answer live.

## The three variants

| | File | Clarifications read as | Writes are surfaced | **Tab bar** |
|---|---|---|---|---|
| **A** | `a-living-document.html` | **Prose, in document flow** — the frozen body under a `captured 07:14 · your words, unchanged` seal, then an `added since` rule, then dated steel-ruled blocks. One continuous piece of writing that grew. | **Anchored to the sentence that caused them** — an indented elbow chip under each paragraph. | **Unchanged**: Note · Attachments · Analysis. Analysis keeps everything it has today. |
| **B** | `b-as-captured.html` | **A version, chosen by a control** — a two-segment `as captured · as it stands (+N)` at the head of the tab. *As captured* is the frozen original alone; *as it stands* adds every dated block, and is what the agent re-reads. | **One chronological strip** under the note — `what this note wrote · 8 · 1 held · 1 failed`, collapsed to a line, expanding to time-ordered rows under time rules. | **Unchanged**: Note · Attachments · Analysis. Analysis keeps everything it has today. |
| **C** | `c-note-and-record.html` | **A folded log** — one row, `1 clarification · added in conversation`, expanding to dated blocks that each quote the question they answered. The note itself opens exactly as captured. | **Collected in a ledger, grouped by entity**, on the Record tab. | **Changes**: Note · Attachments · **Record**. Record **replaces Analysis and absorbs all of it** — generated title + tags, the salient facts (which *are* the write ledger), entity chips, wiki backlinks, and the settled Sources card at its foot. Attachments keeps its name and its manifest. |

### What each one commits you to

- **A — the living document.** *Cost:* the document grows without bound and has no fold, so
  a much-clarified note pushes its own opening line off the screen; and anything with no
  sentence to hang off — a failed call, a truncated pass, a value read only off a photo —
  has to be gathered into a loose tail, which is where A's organising idea stops working.
- **B — as captured / as it stands.** *Cost:* the strip is a **flat chronology**. It
  answers "what happened when" and not "what does this note say about Me" — one entity's
  values are scattered under four time rules and have to be reassembled by the reader.
- **C — the note, and the record.** *Cost:* one story in two places, so the link between
  *what you said* and *what it changed* is a tap rather than a glance; and Record is now a
  big tab carrying both the analysis and the ledger, which is a real consolidation to get
  right rather than a free rename.

## Open questions for the owner

1. **Should the open question appear on the note screen at all**, given questions live in
   the thread and are findable from the inbox's notes tab (D5)? All three currently
   surface it — A and B in the reading flow, C as a tab count **plus** a sentence on the
   Note tab, because a colour-only pill would break the pairing rule.
2. **Can a clarification block be edited or withdrawn?** All three treat it as immutable
   once written; the correction path is another block, or `correct_fact` if you are
   telling the agent it was wrong. If that is wrong, the log's affordances change.
3. **C only:** is folding Analysis into Record the right consolidation, or should Record
   be a fourth tab and Analysis stay as it is? Four tabs is a lot on a phone; two
   overlapping tabs is worse.
4. Should the chip's collapsed line name the **predicate** (`Me · blood pressure`) or only
   the **count** (`Me · 4 edges`)? The mocks show the former on A and B and the latter on
   C's grouped rows — the difference between a scannable margin and a noisy one when a note
   touches six entities.
