---
name: deep_research
version: 1
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
        dispatches web-research sub-agents, so include whatever scope and context
        matter (what you want compared, over what timeframe, from whose perspective).
    breadth:
      type: integer
      description: >-
        Optional. Roughly how many angles to research in the first pass (2–5, default
        4). Higher is more thorough and more expensive; the tool caps it and leaves
        room for a follow-up gap round. Omit to use the default.
  required: [question]
---
Research a question in depth and get back a structured, cited report. Use this for a
genuinely open, multi-source question — one worth planning, gathering across several
angles, checking for gaps, and writing up — NOT a quick lookup (for that, just
`web_search`/`web_fetch` yourself, or a single `spawn_subagent` fan).

Give it one self-contained `question`. The tool does the rest on its own: it plans the
sub-questions, dispatches web-sandboxed research sub-agents to gather and cite sources,
judges whether coverage is enough and fills the biggest gaps with one more round if
not, writes an outlined report, and (for a deep question) has a reviewer critique the
draft and revises it once. It returns the finished report — read it, then present or
build on it. Everything runs from the open web only (no knowledge base), and it is a
single bounded run: it plans and paces itself, so you call it once and wait for the
report rather than steering it mid-run.
