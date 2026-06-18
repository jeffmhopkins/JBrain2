---
name: time_at_place
version: 1
permission: read
domains: [location]
params:
  type: object
  properties:
    place:
      type: string
      description: A saved place name to total time at (e.g. "Home", "Office"). Must match exactly one saved place.
    subject:
      type: string
      description: The person or device to total (e.g. "Jeff's phone"). Omit, or "me", for the owner's own device.
    hours:
      type: number
      description: How far back to look, in hours (default 168 ≈ 1 week, max 8784 ≈ 1 year).
    nights_away:
      type: boolean
      description: When the place is home, also report how many nights (local calendar dates) were spent away from it.
  required: [place]
---
Total how much time a person or device spent at a saved place over a window, and
how many separate visits (e.g. "how long was I at the office this week"). With
nights_away set for a home-style place, also report the nights spent away, bucketed
by the owner's LOCAL calendar date (so it is correct across daylight-saving
changes). OWNER-ONLY: location is owner-only and a narrowed or non-owner session is
refused. The place name must match exactly one saved place — if several match, the
tool ASKS which you mean rather than guessing. The subject must be a linked device
(or a person whose device is linked). Returns names, durations, and counts only —
never a coordinate.
