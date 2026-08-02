---
name: grokipedia_citations
version: 1
permission: web
params:
  type: object
  properties:
    slug:
      type: string
      description: The article slug (from grokipedia_search or grokipedia_outline).
    section:
      type: string
      description: >-
        Optional — a section number or title (from grokipedia_outline) to return only the
        sources cited in that section. Omit for the whole article's citations.
    limit:
      type: integer
      description: Maximum number of citations to return (default 25, max 60).
  required: [slug]
---
Return a Grokipedia article's citations — the numbered list of sources it draws on,
each with a title (when available) and the source URL. This is how you get from a
Grokipedia claim to the PRIMARY source behind it: read a section with grokipedia_section,
then call grokipedia_citations to get the underlying source URLs and web_fetch one to read
the original. Pass a section to narrow to just that section's sources. Grokipedia is
a secondary source — cite and prefer the primary sources you follow from here.
