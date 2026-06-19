---
name: current_location
version: 3
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
position this turn, it returns nothing — say so and ask them to share it. Use the
result to answer what they asked: when it's a named place you may web-search by that
place/city for nearby things; when it's only coordinates, report them and stop — never
web-search raw coordinates to resolve them. Don't volunteer the location unprompted or
repeat the precise address/coordinates beyond what the question needs.
