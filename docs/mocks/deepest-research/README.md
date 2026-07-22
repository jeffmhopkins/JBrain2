# Deepest research — in-flight surface variants (GUI gate)

The R8 GUI gate for `deepest_research` (build plan:
`../../plans/DEEPEST_RESEARCH_TOOL_PLAN.md`). `deepest_research` is a **no-holds
background run** — it recurses two agent tiers deep and can run for an hour — so, unlike
`deep_research`'s live in-turn card, it must be surfaced as a **backgrounded** run that
posts coarse per-round progress into the chat (the run's transcript ticks + a nudge, R6).
This gate decides only **how the running state sits in the chat**; the finished state is
shared.

Open `compare.html` in a browser: the three in-flight variants render in phone frames over
the **same run** (a `deep` question, round 3 of the adaptive loop, 4 task agents fanning
sub-agents), with a dark/light toggle. The **shared finished report** is shown below them.

## The shared contract (heavy reliance on deep_research commonality)

Every variant reuses the shipped `deep_research` visual language wholesale — nothing new is
invented for the machinery:

- the **`fb-drp` timeline** (steel rail + spine, pulsing steel-tint dot, done ✓, the 8
  stages) — the exact component `deep_research` renders live;
- the **`fb-sa` fan** (spark header, steel budget meter, rows with the sweep "working" bar,
  indented `.sub` sub-agent rows);
- the **`deep_research_report` card** for the finished state — `tv-dr` head + provenance
  chips + roster rows (`tv-syn-*`) with per-child session deep-links;
- favicon `[^n]` citations (`md-webcite`), and the **complexity colours** (deep `--violet`,
  comparative `--steel`, simple `--green`).

The only additions are an **amber "deepest" identity badge** (amber = the research accent,
distinguishing a deepest run from a plain deep_research card) and the **two-tier fan**
(task agent → sub agent). The finished report card is **identical across all three** — its
provenance chips just carry the deepest extras (`2 tiers`, task/sub-agent counts, `resumed
once`), and the roster nests sub-agents under their task agent.

| Variant | Treatment | Trade-off |
|---|---|---|
| **A — Backgrounded card** | The full `deep_research` 8-stage timeline verbatim, plus an amber "running in the background" header + a coarse round line (`round 3 · 62 sources · ~70% covered · 24 min`). The stages advance per checkpoint tick, not per token. | **Max familiarity, least risk** — indistinguishable from a live deep_research run. But a full timeline card sits in the thread for a run that may last an hour. |
| **B — Run banner + reopen** | A slim amber-edged banner (question · steel progress bar · `round · sources · coverage · elapsed`) holds the chat; **"View progress" expands** the full timeline on tap. | Keeps an hour-long run from dominating the thread and reads well when the owner moves on to other messages; the detail is one tap away, not always-open. |
| **C — Two-tier emphasis** | Same timeline, but the active stage foregrounds what deepest **adds** over deep_research: the orchestrator → task-agent → sub-agent recursion, with a tier legend + nested (dashed) sub-agent rows. | Most distinct; teaches why the run is "deeper". Slightly busier; leans on the recursion being the interesting story. |

**Recommendation (author):** **A** best honours the "heavy reliance on deep_research
commonality" ask — it *is* the deep_research card, backgrounded — with B's collapse as a
strong hedge if a persistent hour-long card proves too heavy in the thread. C's two-tier
emphasis can be folded into whichever wins (the nested sub-rows are the same `.fb-sa-row.sub`
primitive) rather than shipped as its own layout.

**Chosen: _(pending owner review)._** Once picked, the winner's reasoning folds into
`../../reference/DESIGN.md` (a "Deepest research" section), `compare.html` stays as the
record of the review, and R8 implements the chosen surface in `FullBrainSurface.tsx` +
`styles.css`, reusing the existing `DeepResearchProgress` / `SubagentFan` /
`deep_research_report` components.
