---
name: find_when_at
version: 1
permission: read
domains: [location]
params:
  type: object
  properties:
    place:
      type: string
      description: A saved place name to look up visits for (e.g. "the gym", "Mom's"). Must match exactly one saved place.
    subject:
      type: string
      description: The person or device to look up (e.g. "Jeff's phone"). Omit, or "me", for the owner's own device.
    hours:
      type: number
      description: How far back to look, in hours (default and max 8784 ≈ 1 year).
  required: [place]
---
Find when a person or device was last at a saved place, and how often they visited
over the window (e.g. "when did I last go to the gym"). OWNER-ONLY: location is
owner-only and a narrowed or non-owner session is refused. The place name must match
exactly one saved place — if several match, the tool ASKS which you mean rather than
guessing. With no recorded stays the tool says there are no recorded visits. The
subject must be a linked device (or a person whose device is linked). Returns names,
times, and visit counts only — never a coordinate.
