---
name: show_research_report
version: 1
permission: web
params:
  type: object
  properties:
    id:
      type: string
      description: The id of a saved report (from a list_research_report or search_research_report result).
    question:
      type: string
      description: Alternatively, the exact question the report answered — use this when you don't have an id.
  required: []
---
SHOW one saved deep-research report to the owner as its rich report card — the same
component the run produced: the synthesized report with its cited `[^n]` favicon sources
and provenance chips. Use this when the owner wants to SEE or re-open a past report ("show
me my research on X", "pull up that report"), after list_research_report or
search_research_report has found which one they mean. Pass its id (or the exact question).

Prefer read_research_report when the owner wants the CONTENT in words (to summarize or
answer from it); prefer this when they want the report itself in front of them. The card is
the owner-facing surface — a brief line acknowledging it is enough; don't restate the report.
