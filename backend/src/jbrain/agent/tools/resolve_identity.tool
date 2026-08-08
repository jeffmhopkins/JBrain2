---
name: resolve_identity
version: 1
permission: web
params:
  type: object
  properties:
    name:
      type: string
      description: >-
        The person's name to resolve — a full name works best. Returns the canonical entity(ies)
        with their alternate names.
    limit:
      type: integer
      description: Maximum number of entity matches to return (default 3, max 5).
  required: [name]
---
Resolve a person's identity on Wikidata (FREE, no key) to harvest ALL of their names — the
canonical name plus every alias, aka, maiden, or former name on record — along with occupation,
a short description, and any pivot-useful external IDs (an NPI, if present, feeds
provider_license directly). Use this FIRST when building a person profile or running any
records/license/disciplinary check, because those records are frequently filed under a PRIOR or
MAIDEN name, not the current one.

The workflow: run resolve_identity, pick the entity whose description/occupation matches the
person you mean (a common name can map to several people or even a painting), then RE-RUN the
records searches under EACH alternate name it surfaced — provider_license (for a physician or
clinician), public_records, and federal_register. Not everyone has a Wikidata page; if there is
no match, fall back to the FEC committee name, an official bio, or news to find a prior/maiden
name, then search under each. Treat the entity as a disambiguation aid, not proof of any fact.
