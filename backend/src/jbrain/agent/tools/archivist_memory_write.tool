---
name: archivist_memory_write
version: 1
permission: web
side_effecting: true
params:
  type: object
  properties:
    content:
      type: string
      description: The full updated memory document to save (replaces the previous one). Include your taxonomy decisions, filing rules, progress, and where to resume.
  required: [content]
---
Save your cross-session memory — this REPLACES the whole document, so include
everything you want to keep, not just the new part (read it first, then save the
merged result). Record your taxonomy decisions, filing rules, what you've triaged, and
where to resume, so the next session continues your plan. Keep it a concise, current
summary rather than a running log.
