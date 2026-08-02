---
name: grokipedia_section
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
        Which section to read — its number from grokipedia_outline (e.g. "2.1") or its title
        (e.g. "Early Life"), matched case-insensitively. A parent section includes its
        sub-sections up to the next section of the same or higher level.
    response_format:
      type: string
      enum: [concise, detailed]
      description: >-
        concise (default) returns just the section's text; detailed also appends the
        sources cited within that section and any Grokipedia articles it links to.
  required: [slug, section]
---
Read a single section of a Grokipedia article by its number or title (from
grokipedia_outline), instead of loading the whole page. Returns that section's text as
markdown. Use response_format=detailed to also get the section's citations (to follow
to primary sources) and its linked articles (to traverse). A very long section is
truncated with a pointer to its sub-sections; drill in with grokipedia_outline. This is the
main way to pull specific information out of a Grokipedia article cheaply.
