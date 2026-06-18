---
name: geocode_forward
version: 1
permission: read
domains: [location]
params:
  type: object
  properties:
    query:
      type: string
      description: A free-text place name or address to look up.
    limit:
      type: integer
      description: Maximum candidates to return (default 5).
  required: [query]
---
Forward-geocode a free-text place name or address to candidate coordinates using
the on-box geocoder. The lookup stays on the box (no off-box request). OWNER-ONLY:
a free-text query is an exfiltration channel a typed-parameter allowlist can't
constrain, so a narrowed or non-owner session is refused. Returns up to `limit`
candidates, each as an address line with its coordinates.
