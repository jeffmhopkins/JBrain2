---
name: public_records
version: 4
permission: web
params:
  type: object
  properties:
    name:
      type: string
      description: >-
        The person's name (or organization) to look up. Run once per name VARIANT you know,
        including any prior or maiden name — records are often filed under a former name.
    sources:
      type: array
      items:
        type: string
        enum: [identity, court, license, federal_register]
      description: >-
        Which keyless sources to query (omit for ALL four). identity = Wikidata (canonical name +
        aliases/maiden/former names + occupation; query this FIRST to harvest aliases). court =
        CourtListener (opinions + RECAP dockets + a judges/officials alias lookup). license =
        NPPES/NPI registry (a clinician's license number/state/specialty + other_names).
        federal_register = agency debarments + enforcement notices.
    state:
      type: string
      description: For sources including license — a 2-letter US state to narrow the NPI provider search.
    limit:
      type: integer
      description: Maximum results per source (default 10, max 25; identity caps lower).
  required: [name]
examples:
  - {name: "Neelam Perry", sources: ["identity"]}
  - {name: "Neelam Perry", sources: ["identity", "court", "license", "federal_register"]}
  - {name: "John A. Smith", sources: ["court"]}
---
FREE, keyless public-records lookups about a PERSON by name. ONE tool, four sources — set
`sources` (omit for all four):

- identity — Wikidata: the canonical name + ALL alternate names (aka/maiden/former) + occupation
  + a few pivot IDs (an NPI feeds `license`). Query this FIRST: an alias it finds is a required
  re-search key for the other sources.
- court — CourtListener: U.S. court OPINIONS + RECAP DOCKETS, plus the judges/officials database
  (which links a person filed under a prior name to their canonical record). Free, rate-limited.
- license — NPPES NPI registry: a clinician's NPI, credential, per-state license number +
  specialty, and any other_names (a prior/maiden name). A REGISTRY, not a discipline database —
  a hit is the identifier + alias to take to the right state board, never a clean/dirty verdict.
- federal_register — U.S. federal rules & notices, incl. FDA/agency DEBARMENTS and enforcement
  actions naming a person or company.

Every hit is a LEAD, not a verdict: a common name collides with other people, so verify each
match against its primary document (open the URL with web_fetch — the parties, dates, identifying
detail) before relying on it. For a person profile, run identity first to harvest aliases, then
re-run the other sources under EACH name. Searches by name only (no other owner data leaves the
box) and does not cover every jurisdiction, so an empty result is "not found here", not proof of
no records — fall back to web_search for sources it doesn't index.
