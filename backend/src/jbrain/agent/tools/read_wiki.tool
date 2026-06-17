---
name: read_wiki
version: 1
permission: read
params:
  type: object
  properties:
    article_id:
      type: string
      description: The id of the wiki article to read (from a search "Wiki" result or the landing).
  required: [article_id]
---
Read a machine-written wiki article in full by its id — its lead, its type-guided sections, and
its numbered References. Use this to explain or discuss an article with the owner in the editorial
"Talk" flow, and to cite where a claim came from (each [n] in the prose indexes the References
list, which names the source note, its date, its domain, and the cited snippet).

The wiki is machine-written from the owner's notes and is read-only — never offer to edit it
directly. The owner corrects it by filing a correction note (which out-argues the graph), and the
article is rebuilt from the corrected facts. You only ever see sections this session is scoped to.
