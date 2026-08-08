---
name: research_report
version: 2
permission: web
params:
  type: object
  properties:
    action:
      type: string
      enum: [search, list, read]
      description: >-
        Which library operation. search: find stored reports matching a query. list:
        enumerate/count the whole library (most recent first). read: the FULL markdown of one
        report by id or question.
    query:
      type: string
      description: For action=search — what to look for across the stored reports.
    id:
      type: string
      description: For action=read — the report id from a list/search result.
    question:
      type: string
      description: For action=read — alternatively the exact question a report answered (when you have no id).
    limit:
      type: integer
      description: Max results — search (default 6, max 10) or list (default 20, max 50).
    page:
      type: integer
      description: For action=list — 1-based page to step through a library larger than one page.
  required: [action]
examples:
  - {action: search, query: "Florida Senate primary"}
  - {action: list}
  - {action: read, id: "rr_9f2c"}
---
Read the owner's library of saved deep-research reports — the cited reports the `deep_research`
tool persisted on earlier questions. ONE tool, three read actions — set `action`:

- search — find stored reports matching `query`; returns each match's question, id, and an
  excerpt. Reach for this when the owner asks what a report FOUND.
- list — enumerate/count the WHOLE library (each report's question, complexity, date, most
  recent first, with its id), paged with `page`. The right action for "what have I researched?"
  or "how many reports do I have?".
- read — the FULL markdown of one report by `id` (or its exact `question`). This is how a
  follow-up turn references an earlier run, since chat history keeps only jerv's summary of it.

Typical path: search or list to find a report, then read for its full text. To SHOW a report's
card to the owner, use show_research_report; to REMOVE one, use remove_research_report (those
stay separate tools — this umbrella is read-only). Sandboxed jerv-only over the report corpus's
own scope; it never reads the owner's notes.
