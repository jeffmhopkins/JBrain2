---
name: current_location
version: 1
permission: web
params:
  type: object
  properties: {}
  required: []
---
Get the owner's current location — the saved place they are at (or were last at) and
how fresh that reading is. Call this when the owner asks where they are or wants
something near them (local time, weather, nearby places). It returns a place name
only, never a coordinate, and reports a stale fix as a last-known position rather
than "here now". This is an on-box read of the owner's own location, NOT an internet
call — treat the place as private: use it to answer what they asked, never repeat it
unprompted, and never put it into a web search query or a fetched URL.
