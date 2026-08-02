---
name: grokipedia_related
version: 1
permission: web
params:
  type: object
  properties:
    slug:
      type: string
      description: The article slug (from grokipedia_search or grokipedia_outline).
    limit:
      type: integer
      description: Maximum number of linked articles to return (default 20).
  required: [slug]
---
Return the other Grokipedia articles linked from a given article — a way to traverse
the encyclopedia from one topic to related ones. Each result carries a title and a
slug you can pass straight to grokipedia_outline or grokipedia_section. Use this to broaden from a
subject to adjacent people, organizations, or events without a fresh grokipedia_search.
