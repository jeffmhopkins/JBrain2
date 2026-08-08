---
name: provider_license
version: 1
permission: web
params:
  type: object
  properties:
    name:
      type: string
      description: >-
        The clinician's name to look up — a full name works best. Run once per name VARIANT,
        including any prior or maiden name.
    state:
      type: string
      description: Optional two-letter state code to narrow the search (e.g. "FL").
    limit:
      type: integer
      description: Maximum number of providers to return (default 10, max 25).
  required: [name]
---
Look up a U.S. health-care provider's license in the NPPES NPI Registry (FREE, no key) — the
CMS national registry of clinicians. For each match it returns the NPI, credential, status, the
per-state license number and specialty, and — the key output — any OTHER NAMES on file (a prior
or maiden name). Use this for any physician, nurse, therapist, or other licensed clinician: it
is the national replacement for scraping one state's DOH portal, and its other_names field is
often the pivot to the RIGHT state board.

This is a REGISTRY, not a discipline database: it gives you the license number, state, and
specialty, but it does NOT list disciplinary actions. So a hit is an identifier and an alias,
never a clean-or-dirty verdict — take the license number + state (and each other_name) to that
state's licensing board to check for discipline. Common names collide, so confirm the match is
really THIS person. If nothing is found, try a name variant or drop the state filter, since the
NPI may be filed under a former name or a different state. Pair with resolve_identity (to find
aliases first) and public_records.
