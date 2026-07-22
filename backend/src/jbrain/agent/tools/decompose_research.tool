---
name: decompose_research
version: 1
permission: web
cost_class: expensive
params:
  type: object
  properties:
    subtopics:
      type: array
      description: >-
        The 2–3 independent sub-briefs to research in parallel, one per sub agent. Each
        must be self-contained — the sub agent that works it sees ONLY that brief, never
        your question or its siblings — and genuinely independent of the others (they run
        at the same time in isolation). Only split a sub-question that has separable,
        substantial parts; a focused question you research yourself.
      items:
        type: object
        properties:
          title:
            type: string
            description: A SHORT label for this sub-brief (3–6 words) — the sub agent's row heading.
          brief:
            type: string
            description: >-
              The full, self-contained research instruction the sub agent works. Restate
              all the context it needs; never reference the other sub-briefs or your
              question.
        required: [brief]
  required: [subtopics]
---
Split your assigned sub-question into a small fan of independent sub agents, each of
which researches one sub-brief and returns a cited summary you get back to fold into
your own. Use this ONLY for a genuinely compound sub-question with separable, substantial
parts — for a focused question, research it yourself; delegation is not free.

You may call this at most ONCE per run: read your sub agents' findings and write your
unified summary rather than decomposing again. It is available only to a task agent
inside a deepest-research run; it does nothing in an ordinary turn.
