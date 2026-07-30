---
name: deep_produce
version: 2
permission: read
cost_class: expensive
params:
  type: object
  properties:
    question:
      type: string
      description: >-
        The topic to research, as a complete, self-contained question — the tool plans its
        own sub-questions from it and dispatches research sub-agents, so include whatever
        scope and context matter (what to cover, over what timeframe, from whose perspective).
    output_kind:
      type: string
      enum: [report, plan, table, brief, differential, timeline]
      description: >-
        What artifact to produce. `report` (default) is the same cited write-up `deep_research`
        gives. `plan` is a decision-oriented, actionable plan; `table` a comparison table with a
        short synthesis; `brief` a tight bottom-line-up-front summary; `differential` the
        candidate explanations with evidence for/against each; `timeline` a dated chronological
        account. Every kind is still a cited Markdown document. Omit for `report`.
    objective:
      type: string
      description: >-
        Optional. A one-line directive shaping WHAT to produce, beyond the raw question — e.g.
        "an idealized step-by-step plan if the symptoms recur" or "compare on cost, risk, and
        time-to-value". It steers the write-up over the default report shape. Omit to let the
        question stand as the objective.
    breadth:
      type: integer
      description: >-
        Optional. Roughly how many angles to research in the first pass (2–5, default 4). Higher
        is more thorough and more expensive; the tool caps it and leaves room for a follow-up gap
        round. Omit for the default.
    sources:
      type: string
      enum: [web, library, library_first]
      description: >-
        Optional. Where the research draws from. `web` (default) researches the open web.
        `library` researches ONLY the owner's analysed-video library — no web at all.
        `library_first` makes the library the primary pass and lets the web fill only what it is
        missing. Omit for `web`. IMPORTANT: to produce something FROM a specific video the owner
        has analysed (e.g. a table of every question in an interview, the claims in a talk), you
        MUST use `library_first` (or `library`) — that video's transcript lives only in the
        library, so `web` cannot read it. Never pass `web` for a task about a specific analysed
        video, even when part of it (fact-checking) needs the web: `library_first` extracts from
        the video and still lets the later steps reach the web.
    mode:
      type: string
      enum: [standard, deepest]
      description: >-
        Optional. `standard` (default) runs the bounded pipeline: one gap round, one critique.
        `deepest` keeps looping the gap-filling round until the topic is genuinely covered and
        the findings stop changing (or a large internal ceiling is hit) — materially more
        thorough and more expensive. Reserve `deepest` for a big, open topic; omit for `standard`.
  required: [question]
---
Research a topic in depth and get back a structured, cited artifact of the kind you ask for —
a report, a plan, a comparison table, a brief, a differential, or a timeline. Use this when the
useful output is something other than a prose report (say, "give me a step-by-step plan for X"
or "compare these options in a table"), for a genuinely open, multi-source topic worth planning,
gathering across several angles, checking for gaps, and writing up — NOT a quick lookup (for
that, just `web_search`/`web_fetch` yourself).

It is `deep_research` with a choice of output: give it one self-contained `question`, set
`output_kind` to the artifact you want, and optionally an `objective` line to steer the shape.
The tool does the rest on its own — plans the sub-questions, dispatches sandboxed research
sub-agents to gather and cite sources, judges coverage and fills the biggest gaps, writes the
artifact, has a reviewer critique it, and revises once. Every kind is a cited Markdown document
held to the same sourcing discipline as a report. It is a single bounded run: you call it once
and wait for the finished artifact rather than steering it mid-run.

By default it researches the open web. Set `sources` to draw on the owner's analysed-video
library instead (`library` for library-only, `library_first` to prefer the library and let the
web fill gaps), for a topic the owner wants answered against their videos.
