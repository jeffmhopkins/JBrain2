---
name: list_research_report
version: 1
permission: web
params:
  type: object
  properties:
    limit:
      type: integer
      description: How many reports to list per page (default 20, max 50).
    page:
      type: integer
      description: Which page to list (1-based, default 1) — step through a large library. The result reports "Page X of Y" and the exact total.
  required: []
---
List the owner's library of saved deep-research reports and report its exact total — the
whole library, not a search. Use this to answer "what have I researched?", "how many
reports do I have?", or to browse/page the catalogue (each report's question, complexity,
and date, most recent first), with the id to read or re-open each. This is the right tool
for enumerating or counting; reach for search_research_report when the owner asks about
what a report FOUND. The total and page count come back exact; step through a library
larger than one page with `page`.
