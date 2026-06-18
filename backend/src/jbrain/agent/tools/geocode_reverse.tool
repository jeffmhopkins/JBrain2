---
name: geocode_reverse
version: 1
permission: read
domains: [location]
params:
  type: object
  properties:
    latitude:
      type: number
      description: Latitude in decimal degrees.
    longitude:
      type: number
      description: Longitude in decimal degrees.
  required: [latitude, longitude]
---
Reverse-geocode a coordinate to a street address using the on-box geocoder. The
lookup stays on the box (no off-box request, nothing staged for approval) and runs
within this session's scope. Returns the nearest address as a single line, or a
note that no address was found. Useful for naming where a location fix or geofence
sits in plain words.
