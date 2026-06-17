---
name: request_rebuild
version: 1
permission: mutate
params:
  type: object
  properties:
    article_id:
      type: string
      description: The wiki article to rebuild.
  required: [article_id]
---
Queue a full rebuild of one wiki article — re-derive it from the current facts and sources. Use
this when the owner asks to refresh an article, or after filing a correction or source exclusion
if they want it reflected promptly. The rebuild runs in the background; confirm it is queued.
