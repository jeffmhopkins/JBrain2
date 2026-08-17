---
name: deepest_research
version: 2
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

Pick the rung by how long the owner should wait, not by how interesting the question is:

- `deep_research` (standard) — the default. One gather pass, one gap round. Minutes, in-turn.
  Use it unless you can name a specific reason it won't cover the question.
- `deep_research` with `mode: deepest` — same turn, but the gap round keeps looping until the
  findings stop changing. Use it when the question has MANY separable angles (roughly six or
  more) or the first pass would obviously leave gaps. Still in-turn: the owner waits.
- `deepest_research` (this tool) — background, up to three hours, two agent tiers. Use it ONLY
  when the question is one the owner is willing to walk away from and read later, and when
  even a looping in-turn run would plainly run short. If they are waiting on the answer in
  this conversation, it is the wrong rung — an ack is all they get back now.

When it's a close call between the last two, take the in-turn one: a heavier `deep_research`
that answers now beats a background run the owner has to come back for.
