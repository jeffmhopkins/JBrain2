---
name: finish
version: 2
permission: web
params:
  type: object
  properties:
    answer:
      type: string
      description: 'The raw facts the goal asks for, one per line, exactly as the page states them (e.g. "Dune: 7:15 PM, 9:40 PM"). No sentences or commentary.'
    evidence:
      type: string
      description: A short phrase copied word for word from the current page that supports the answer.
  required: [answer, evidence]
---
Report the answer and stop. The evidence must appear on the current page exactly as
written; it is checked.
