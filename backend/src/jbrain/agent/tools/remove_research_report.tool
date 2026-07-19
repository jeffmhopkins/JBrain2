---
name: remove_research_report
version: 1
permission: web
params:
  type: object
  properties:
    id:
      type: string
      description: The id of a saved report to remove (from a list_research_report or search_research_report result).
    question:
      type: string
      description: Alternatively, the exact question the report answered — use this when you don't have an id.
  required: []
---
Remove one saved deep-research report from the owner's library. This does NOT delete
anything itself — it stages the removal for the owner to approve inline, and only their
approval deletes it (permanently). Use it when the owner asks to remove, delete, or forget
a report from their library; find the right one with list_research_report or
search_research_report first, then pass its id (or the exact question).

Report that you've staged the removal and that nothing is deleted until they approve —
don't claim the report is gone. One tool call stages one report's removal.
