---
name: gmail_count
version: 1
permission: web
params:
  type: object
  properties:
    query:
      type: string
      description: A Gmail search query, e.g. "from:chase.com", "from:(*@chase.com)", "older_than:10y label:newsletters".
  required: [query]
---
Count how many messages match a Gmail query — the exact total, not a page. Use it to
size a sender or domain before acting ("how many emails from chase.com?") and ALWAYS
call it before a gmail_bulk_label so you know, and can state, the blast radius of a
bulk move. Match a whole domain with Gmail's own syntax, e.g. from:chase.com or
from:(*@chase.com). Very large counts are reported as "at least N".
