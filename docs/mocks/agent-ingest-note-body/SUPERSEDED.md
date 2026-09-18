# Superseded — this round was scrapped before the gate

> **Status:** Superseded · **Scrapped:** 2026-09-08 · never presented to the owner, never merged.
>
> ⟲ **Its scrapping premise — "the note screen does not change" — was reversed by the owner
> on 2026-09-14.** The note screen changes: its tabs are **Note · Files** and the Analysis
> tab folded into `Note`. The conversation, though, does live elsewhere after all — not on
> the conversations surface this round assumed, but on **Entry**, whose default view is the
> notes list and whose main view becomes the selected note's thread
> (`docs/mocks/agent-ingest/README.md`, *Where it ended up*). In his own words: *"I want you
> to keep the one omnibox just like jerv. The difference is the default view of entry would
> be notes. And when you select a note, it basically loads a conversation the same as if I
> had swiped left inside of jerv and picked a different conversation."* What survives is
> **why this round was scrapped anyway**: it asked the note screen to grow a *bespoke*
> ingest rendering — a Record tab, an as-captured toggle, prose-woven clarifications — and
> the answer built instead is the ORDINARY agent transcript. The question below is still the
> wrong question.

**The question was wrong.** These three variants redesign the **note screen** with a
bespoke ingest treatment. A note conversation is an ordinary agent conversation — the same
surface as Full Brain / Jervis chat, with the note as turn 0 (plan
`docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md` **D1**, "one agent, one conversation type").
The agent's graph writes and its clarifying questions render as **custom tool components
inside that conversation**, so the Note tab keeps rendering the body as it does today: no
Record tab, no as-captured/as-it-stands toggle, no prose-woven clarifications. **D6**'s
appended clarification blocks were always a *storage* decision — they keep the graph citable
and re-derivable from notes alone — and the existing note view already renders appended text
as text.

*(Since 2026-09-14 that conversation is hosted by **Entry** — tap a note in the list and it
loads in the main view — and the Analysis tab's content folded into the note screen's `Note`
tab. Neither is a bespoke ingest rendering: Entry mounts the shipped `AgentTranscript`, and
`Note` gained the existing Analysis component unchanged.)*

**Two findings from this round carry forward into the tool-component round**, and are the
reason these files are kept rather than deleted:

- **The chip anatomy is right wherever it lives.** A graph-write component has to render
  **written · replaced · held · from a photo · failed · truncated · writing…**, with the
  staged in-flight rendering (appended → re-chunked/re-embedded → re-reading the note → N
  articles to rebuild) that shows the real serial-GPU wait, and **per-row domain provenance
  in words**, never colour alone. See frame 2 of any of the three files.
- **An answered question is an ordinary assertion**, sourced to its clarification block —
  **never a `correction`**. `correction` is force-supersede **and pin**, reserved for
  `correct_fact` when the owner disagrees; pinning every answered clarification would put
  facts permanently beyond the settle sweep.
