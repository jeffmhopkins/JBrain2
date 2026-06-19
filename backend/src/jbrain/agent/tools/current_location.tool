---
name: current_location
version: 4
permission: web
params:
  type: object
  properties:
    precise:
      type: boolean
      description: Set true only when the owner wants their exact street address; otherwise omit for a city-level answer.
  required: []
---
Get the owner's current location, resolved from the position their app captured this
turn. By default it names the nearest city (city, region, country) — enough for "where
am I" and nearby info. Set `precise: true` ONLY when the owner explicitly wants their
exact street address; if no address can be resolved it returns the coordinates. If
their app didn't share a position this turn, it returns nothing — say so and ask them
to share it. Use the result to answer what they asked: when it names a place you may
web-search by that place/city for nearby things; when it's only coordinates, report
them and stop — never web-search raw coordinates to resolve them. Don't volunteer the
location unprompted or repeat the precise address/coordinates beyond what's needed.
