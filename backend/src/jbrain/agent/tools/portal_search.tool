---
name: portal_search
version: 2
permission: web
params:
  type: object
  properties:
    name:
      type: string
      description: >-
        The entity or person name to look up in the portal. Run once per name VARIANT you know
        (a prior, DBA, or alternate name) — records are often filed under a different name.
    jurisdiction:
      type: string
      description: >-
        The portal's jurisdiction, e.g. "FL". Pair with kind to pick a portal. This build ships
        FL/business (Sunbiz corporation registry) and FL/license (DFS licensee lookup); a box may
        have more adapters wired, and calling with no jurisdiction/kind lists whatever it has.
    kind:
      type: string
      description: >-
        What KIND of portal to query — "business" (a state business/corporation registry) or
        "license" (a state professional-license lookup). Pair with jurisdiction.
    key:
      type: string
      description: >-
        Optional explicit resolver key (e.g. "fl_business") to target one portal directly instead
        of jurisdiction + kind.
    limit:
      type: integer
      description: Maximum results (default 10, max 25).
  required: [name]
examples:
  - {name: "Property Pros Consulting", jurisdiction: "FL", kind: "business"}
  - {name: "Frank Collige", jurisdiction: "FL", kind: "license"}
  - {name: "Acme Holdings LLC", key: "fl_business"}
---
Actually QUERY a dynamic government search portal by name — the ones a plain web_fetch can only
see the empty search FORM of (a state business registry, a state license lookup). Pass a name plus
a jurisdiction + kind (e.g. jurisdiction="FL", kind="business") and portal_search runs the
portal's real search, then returns each result with a STATIC detail-page URL you can web_fetch and
cite. Use this when you need a STATE portal's own records; use public_records for the NATIONAL,
keyless registries (NPPES/NPI licenses, CourtListener, Wikidata, Federal Register) instead — they
are different sources. Every hit is a LEAD, not a verdict: a common name collides with other
entities, so open the detail URL with web_fetch and confirm the parties/identifiers before relying
on it. Searches by name only (no other owner data leaves the box) and covers only the portals this
instance has adapters for, so an empty result is "not found here", not proof no record exists —
try a name variant, or web_search for portals it doesn't cover. If the portal is down the tool says
so; that too is not evidence of absence.
