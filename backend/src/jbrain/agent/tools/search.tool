---
name: search
version: 2
permission: read
params:
  type: object
  properties:
    query:
      type: string
      description: What to look for in the owner's notes.
    limit:
      type: integer
      description: Maximum number of results (default 8).
  required: [query]
---
Search the owner's knowledge base — their notes and the passages drawn from them —
and return the most relevant matches. Use this first to ground an answer in the
owner's own data. Each result shows the source note id (pass it to read_note for
the full note), its domain, the date, and a snippet. You only ever see notes this
session is scoped to.

A note's text is the original record at capture time, not necessarily what is true
now. When a result's note contains facts that have since been superseded, retracted,
or left pending review, the result carries a ⚠ currency flag naming the entity ids
whose current values you should confirm with read_entity before relying on that
note's claims.
