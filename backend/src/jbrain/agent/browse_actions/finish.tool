---
name: finish
version: 1
permission: web
params:
  type: object
  properties:
    answer:
      type: string
      description: The answer to the goal, in plain sentences, with the specifics as the page states them.
    evidence:
      type: string
      description: A short phrase copied word for word from the current page that supports the answer.
  required: [answer, evidence]
---
Report the answer and stop. The evidence must appear on the current page exactly as
written; it is checked.
