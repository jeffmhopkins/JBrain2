---
name: public_records
version: 2
permission: web
params:
  type: object
  properties:
    name:
      type: string
      description: >-
        The exact name (or alias) to search — a person's full name, or an organization.
        Run this once per name VARIANT you know, including any prior or maiden name.
    limit:
      type: integer
      description: Maximum number of records to return (default 10, max 25).
  required: [name]
---
Search FREE public court records by name. v1 covers CourtListener — U.S. court
OPINIONS (decided cases) and RECAP DOCKETS (filings) from the Free Law Project, a free,
rate-limited source. Use this to find court or disciplinary matters filed under a name
that a plain web_search misses — for a person profile, run it once per name VARIANT you
have, especially any PRIOR or MAIDEN name, since records are often filed under a former
name. Each case result is "CaseName — Court (dateFiled) [docket] URL". It also searches
CourtListener's judges/officials database and returns any matching people with their POSITIONS
and, when present, an ALIAS link — a person filed under a prior name is tied there to their
canonical record, which you can follow and re-search under that name.

Every hit is a LEAD, not a verdict. A common name can belong to a different person, so
you MUST verify each match is really THIS individual before relying on it: open the
record's URL with web_fetch and confirm it against the primary document (the parties,
dates, and identifying detail). Do not report a record as this person's until you have
confirmed it. This tool only surfaces candidates; web_fetch and your judgment confirm
them. It searches by name only — it sends no other owner data — and it does not cover
every jurisdiction, so a clean result is not proof of no records; fall back to web_search
for sources it doesn't index.
