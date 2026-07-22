---
name: deepest_research
version: 1
permission: web
cost_class: expensive
params:
  type: object
  properties:
    question:
      type: string
      description: >-
        The question to research EXHAUSTIVELY. Write it as a complete, self-contained
        question with all the scope and context that matter — the run plans its own
        sub-questions, dispatches a two-tier fan of research agents, and loops until the
        topic is genuinely covered.
    budget_tokens:
      type: integer
      description: >-
        Optional. A LOWER per-run token ceiling than the default. A run may only ask for
        less, never more; omit for the standard (large) ceiling.
  required: [question]
---
Kick off an EXHAUSTIVE, no-holds background research run and get an acknowledgement back
immediately — NOT the report. Use this only for a genuinely large, open question that a
single bounded `deep_research` run clearly won't cover and that is worth minutes-to-hours
of work: it recurses two agent tiers deep, loops until the topic is covered (or a large
owner-set ceiling is hit), and writes a cited report.

This is enqueue-and-return: it starts the run in the background and hands your turn back at
once. Progress ticks and the finished report arrive asynchronously in THIS chat — do not
wait on it or call it again. Only one deepest run may be in flight at a time; if one is
already going, this reports that rather than starting a second.

For anything a bounded run can handle, use `deep_research` (or `deep_research` with
`mode: deepest` for a heavier in-turn run) instead — this is the escalation for the rare
question that truly earns an hour of research.
