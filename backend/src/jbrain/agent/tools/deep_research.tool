---
name: deep_research
version: 5
permission: web
cost_class: expensive
params:
  type: object
  properties:
    question:
      type: string
      description: >-
        The research question to investigate in depth. Write it as a complete,
        self-contained question — the tool plans its own sub-questions from it and
        dispatches research sub-agents, so include whatever scope and context
        matter (what you want compared, over what timeframe, from whose perspective).
    breadth:
      type: integer
      description: >-
        Optional. Roughly how many angles to research in the first pass (2–5, default
        4). Higher is more thorough and more expensive; the tool caps it and leaves
        room for a follow-up gap round. Omit to use the default.
    sources:
      type: string
      enum: [web, library, library_first]
      description: >-
        Optional. Where the research draws from. `web` (default) researches the open
        web. `library` researches ONLY the owner's analysed-video library (the corpus
        of videos they've analysed) — no web at all; use it for "what do my videos say
        about X". `library_first` makes the library the primary pass and lets the web
        fill only what the library is missing. Omit for `web`. To research a question
        ABOUT a specific analysed video (its transcript lives only in the library, not on
        the web), use `library_first` (or `library`) — never `web`, which cannot read it.
    mode:
      type: string
      enum: [standard, deepest]
      description: >-
        Optional. `standard` (default) runs the bounded pipeline: one gap round, one
        critique. `deepest` keeps looping the gap-filling round until the question is
        genuinely covered and the findings stop changing (or a large internal ceiling is
        hit) — materially more thorough and more expensive. Reserve `deepest` for a big,
        open question that a single gap round clearly won't cover; omit for `standard`.
    preset:
      type: string
      description: >-
        Optional. The name of a saved report PRESET (a checked-in template that pins the
        section outline and the research angles) so the report comes out in a fixed, uniform
        shape. When set, the tool skips its own planning and runs the preset's fixed plan,
        filling the preset's `{{variables}}` from the `variables` object — use it to get
        identical-structured reports across many subjects (e.g. `candidate_profile` for every
        candidate on a ballot). Omit to let the tool plan the report itself (the default).
    variables:
      type: object
      description: >-
        Optional. The values that fill a `preset`'s `{{variables}}` for this run — e.g.
        {"candidate": "Jane Doe", "office": "U.S. Senate (Florida)"}. Required whenever the
        chosen preset declares variables; a missing one is refused rather than left blank.
        Ignored when no `preset` is given. With a preset, `question` is optional (the preset
        derives it).
  required: []
---
Research a question in depth and get back a structured, cited report. Use this for a
genuinely open, multi-source question — one worth planning, gathering across several
angles, checking for gaps, and writing up — NOT a quick lookup (for that, just
`web_search`/`web_fetch` yourself, or a single `spawn_subagent` fan).

Give it one self-contained `question`. The tool does the rest on its own: it plans the
sub-questions, dispatches sandboxed research sub-agents to gather and cite sources,
judges whether coverage is enough and fills the biggest gaps with one more round if
not, writes an outlined report, and (for a deep question) has a reviewer critique the
draft and revises it once. It returns the finished report — read it, then present or
build on it. It is a single bounded run: it plans and paces itself, so you call it once
and wait for the report rather than steering it mid-run.

By default it researches the open web (no owner knowledge base). Set `sources` to point
it at the owner's analysed-video library instead: `library` for a library-only run
(nothing from the web — the answer is what their videos say, cited to video +
timestamp), or `library_first` to research the library first and let the web fill only
what the library is missing. Reach for these when the owner asks about their videos or
wants a question answered against their library.
