---
name: nearby_now
version: 1
permission: read
domains: [location]
params:
  type: object
  properties:
    radius_m:
      type: number
      description: Search radius in meters around the owner's current position (default 1000, max 50000).
    limit:
      type: integer
      description: Maximum nearby places to return (default 5, max 20).
  required: []
---
List the saved places within a bounded radius of the owner's current position,
nearest first, with the approximate distance to each. OWNER-ONLY: location is
owner-only and a narrowed or non-owner session is refused. Requires the owner's
device to be linked. The owner's position is used only to compute distances — it
is never returned. Returns place names and coarse distances — never a coordinate.
