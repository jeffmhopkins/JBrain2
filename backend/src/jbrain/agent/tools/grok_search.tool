---
name: grok_search
version: 1
permission: web
params:
  type: object
  properties:
    query:
      type: string
      description: What to look up on Grokipedia — a topic, person, event, or subject.
    limit:
      type: integer
      description: Maximum number of articles to return (default 6, max 10).
  required: [query]
---
Search Grokipedia (xAI's encyclopedia) for articles matching a query, returning
each match's title, its slug, view count, and a short snippet. Use this first to
find the right article on a subject, news topic, or current event, then pass the
slug to grok_outline (to see the article's sections) or grok_section (to read one).
Grokipedia is machine-written and can lag current events by weeks — treat it as a
fast overview with citations to follow, and use web_search for breaking news. Prefer
this over web_search when you want an encyclopedic article on a topic with sources
you can trace to primary material.
