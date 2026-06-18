---
name: where_is
version: 1
permission: read
domains: [location]
params:
  type: object
  properties:
    subject:
      type: string
      description: The name of a person or device to locate (e.g. "Jeff's iPhone").
  required: [subject]
---
Report where a named person or device currently is, by their saved place plus the
freshness of the last fix. OWNER-ONLY: location is owner-only and a narrowed or
non-owner session is refused. The subject must be a linked device (or a person
whose device is linked, via the owner-set device binding); an unlinked or unknown
subject is reported as such. Returns the place name and when it was last seen, and
FLAGS a stale fix as a last-known position — never a coordinate, never "here now"
for an old fix.
