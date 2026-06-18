---
name: location_history
version: 1
permission: read
domains: [location]
params:
  type: object
  properties:
    subject:
      type: string
      description: The person or device whose history to map (e.g. "Jeff's phone"). Omit, or "me", for the owner's own device.
    hours:
      type: number
      description: How far back to look, in hours (default 24, max 744 ≈ 31 days).
  required: []
---
Map a person or device's recent location history as a trail. OWNER-ONLY: location
is owner-only and a narrowed or non-owner session is refused. The subject must be a
linked device (or a person whose device is linked); an unlinked or unknown subject
is reported as such. The answer leads with a prose summary (distance, time span, and
any GPS gaps spelled out in words) and attaches a map view. A GPS gap is never drawn
across — the trail splits into separate legs at each gap. Returns names, times, and
distances only — never a coordinate (coordinates render only on the map).
