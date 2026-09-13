# Superseded — this round was scrapped before the gate

> **Status:** Superseded · **Scrapped:** 2026-09-08 · never presented to the owner, never merged.

**The question was wrong.** These three variants redesign the **note screen**, and the note
screen does not change. A note conversation is an ordinary agent conversation — the same
surface as Full Brain / Jervis chat, with the note as turn 0 (plan
`docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md` **D1**, "one agent, one conversation type").
The agent's graph writes and its clarifying questions render as **custom tool components
inside that conversation**, so the Note tab keeps rendering the body as it does today and
Attachments and Analysis are untouched: no Record tab, no as-captured/as-it-stands toggle,
no prose-woven clarifications. **D6**'s appended clarification blocks were always a
*storage* decision — they keep the graph citable and re-derivable from notes alone — and
the existing note view already renders appended text as text.

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
