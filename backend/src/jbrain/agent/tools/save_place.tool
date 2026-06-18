---
name: save_place
version: 1
permission: mutate
domains: [location]
params:
  type: object
  properties:
    name:
      type: string
      description: >-
        What to call this place — the name the owner will use to ask about it
        later ("Home", "the gym", "Mom's house"). Becomes the Place's name.
    radius_m:
      type: number
      description: >-
        Optional geofence radius in meters (how big the place is). Defaults to a
        building-sized fence; clamped to a sane range. Use a larger value for a
        campus or park, a smaller one for a single room or storefront.
  required: [name]
---
Save the owner's CURRENT location as a named place. OWNER-ONLY: location is
owner-only and a narrowed or non-owner session is refused. Requires the owner's
own device to be linked and to have reported a recent enough fix; otherwise it
declines rather than fence a stale or unknown spot.

This NEVER writes a place directly — you have no privileged write path into the
graph or the place mirror. It STAGES a Proposal the owner approves; on approval a
place-note re-enters through the normal note pipeline, and the existing extraction
+ projection turn it into a Place with a geofence. Tell the owner you've staged it
for review. Do not put coordinates in your reply — name the place and its rough
size; the exact position lives in the note the owner approves.
