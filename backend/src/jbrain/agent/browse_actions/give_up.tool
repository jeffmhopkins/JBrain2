---
name: give_up
version: 1
permission: web
params:
  type: object
  properties:
    reason:
      type: string
      description: One sentence on why the goal cannot be done here.
  required: [reason]
---
Stop without an answer — the site blocks you, the page does not exist, or the goal cannot
be done on this site.
