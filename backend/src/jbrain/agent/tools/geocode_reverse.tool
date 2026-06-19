---
name: geocode_reverse
version: 2
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
Name a coordinate by its NEAREST city, using the on-box offline geocoder. The lookup
stays on the box (no off-box request, nothing staged for approval) and runs within
this session's scope. Returns the nearest city/region/country and roughly how far the
coordinate is from it — city-level, not a street address — or a note that no populated
place is near. Useful for naming where a location fix or geofence sits in plain words.
