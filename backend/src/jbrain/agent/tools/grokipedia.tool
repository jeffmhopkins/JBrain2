---
name: grokipedia
version: 2
permission: web
params:
  type: object
  properties:
    action:
      type: string
      enum: [search, outline, section, citations, related]
      description: >-
        Which Grokipedia operation to run. search: find articles by query. outline: an
        article's table of contents by slug. section: read one section by slug + section.
        citations: an article's (or a section's) source URLs by slug. related: articles
        linked from one, by slug.
    query:
      type: string
      description: For action=search — a topic, person, event, or subject to look up.
    slug:
      type: string
      description: For action=outline/section/citations/related — the article slug from a search (e.g. Elon_Musk).
    section:
      type: string
      description: >-
        For action=section (required) or citations (optional) — the section number (e.g. "2.1")
        or its title (e.g. "Early Life"), matched case-insensitively. A parent section includes
        its sub-sections.
    limit:
      type: integer
      description: >-
        Max results — search (default 6, max 10), citations (default 25, max 60), or related
        (default 20).
    response_format:
      type: string
      enum: [concise, detailed]
      description: >-
        For action=section — concise (default) returns just the section text; detailed also
        appends the section's cited sources and any linked Grokipedia articles.
  required: [action]
examples:
  - {action: search, query: "Chagos Archipelago sovereignty dispute"}
  - {action: section, slug: "Elon_Musk", section: "Early life"}
  - {action: citations, slug: "Ashley_Moody"}
---
Query Grokipedia (xAI's encyclopedia). ONE tool, five actions — set `action`:

- search — find articles matching `query`; returns each match's title, slug, view count, and a
  snippet. Start here to get a slug.
- outline — an article's table of contents (section headings as a numbered tree) by `slug`,
  WITHOUT the body. The cheap way to see how a long article is organized before reading.
- section — read a single section by `slug` + `section` (number or title), instead of the whole
  page. `response_format=detailed` also appends that section's citations and linked articles.
- citations — the numbered source list (title + URL) an article draws on, by `slug`; pass
  `section` to narrow to one section's sources. This is how you get from a Grokipedia claim to
  the PRIMARY source: read a section, then citations, then web_fetch a source URL.
- related — the other Grokipedia articles linked from one, by `slug` — traverse to adjacent
  people/orgs/events without a fresh search.

Typical path: search → outline → section → citations (or related) on one slug (the article is
cached per slug, so a drill-down after the first fetch is free). Runs DIRECTLY like
web_search/web_fetch (a pinned public site, no owner data). Grokipedia is machine-written and can
lag current events by weeks — treat it as a fast, citation-backed overview, follow citations to
primary sources, and use web_search for breaking news.
