---
name: current_location
version: 2
permission: web
params:
  type: object
  properties: {}
  required: []
---
Get the owner's current location — a city/street address resolved (on-box) from the
position their app captured this turn, or the coordinates themselves when it can't be
named. Call this when the owner asks where they are or wants something near them
(local weather, nearby places, their local time). If their app didn't share a
position this turn, it returns nothing — say so and ask them to share it. This is the
owner's live whereabouts: use it to answer what they asked — including a web search
for nearby things (e.g. by the city) — but don't volunteer it unprompted or repeat
the precise address/coordinates beyond what the question needs.
