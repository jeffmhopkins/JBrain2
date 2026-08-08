---
name: federal_register
version: 1
permission: web
params:
  type: object
  properties:
    query:
      type: string
      description: >-
        The search term — a person or company name, or a name paired with a term like
        "debarment". Run once per name VARIANT.
    limit:
      type: integer
      description: Maximum number of documents to return (default 10, max 25).
  required: [query]
---
Search the Federal Register (FREE, no key) — the daily journal of U.S. federal rules and
notices, including FDA and other agency DEBARMENTS and enforcement actions. Each result gives
the document title, the issuing agency, the publication date, a matched excerpt, and a URL. Use
this to catch a debarment or enforcement notice that names a person or company — search a
surname plus "debarment", or a company name — which a plain web_search often misses.

Every hit is a LEAD, not a verdict: open the notice with web_fetch and confirm it names THIS
person or entity (and read the actual action and its status) before relying on it. Common names
collide, so run this once per name variant, including any prior or maiden name (pair with
resolve_identity to harvest those first).
