---
name: location_query
version: 1
permission: read
domains: [location]
params:
  type: object
  properties:
    place:
      type: string
      description: A saved place name or address to aggregate the owner's fixes at (e.g. "Walmart", "Home").
    hours:
      type: number
      description: The window to look back over, in hours (default 24, max 744 ≈ 31 days).
    radius_m:
      type: number
      description: Match radius around the place in meters when it has no saved fence (default 150, max 50000).
  required: [place]
---
Answer an aggregate question about the owner's own device at a place over a time
window — how many fixes there, the battery range, the mean accuracy (e.g. "battery
at Walmart last night"). OWNER-ONLY: location is owner-only and a narrowed or
non-owner session is refused. The place resolves to a saved fence first; on a miss
it forward-geocodes the text on-box (a local read, never sent off the box). Requires
the owner's device to be linked. Returns the place name and the aggregate numbers
plus a map view — never a coordinate in the text.
