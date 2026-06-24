---
name: current_location
version: 5
permission: web
params:
  type: object
  properties:
    detail:
      type: string
      enum: [city, address, coordinates]
      description: How to report the location. "city" (default) names the nearest city; "address" returns the exact street address (needs an external geocoder, falls back to city otherwise); "coordinates" returns the raw latitude/longitude of this turn's fix.
  required: []
---
Get the owner's current location, resolved from the position their app captured this
turn. By default (or `detail: "city"`) it names the nearest city (city, region,
country) — enough for "where am I" and nearby info. Use `detail: "address"` ONLY when
the owner explicitly wants their exact street address; if no address can be resolved it
returns the city. Use `detail: "coordinates"` when the owner wants the raw
latitude/longitude. If their app didn't share a position this turn, it returns nothing
— say so and ask them to share it. Use the result to answer what they asked: when it
names a place you may web-search by that place/city for nearby things; when it's
coordinates, report them and stop — never web-search raw coordinates to resolve them.
Don't volunteer the location unprompted or repeat the precise address/coordinates
beyond what's needed.
