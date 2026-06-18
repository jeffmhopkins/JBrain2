---
name: home_status
version: 1
permission: read
domains: [location]
params:
  type: object
  properties: {}
---
Report who is currently at which saved place across the household — each linked
subject's current place cross-checked against how fresh its last fix is.
OWNER-ONLY: location is owner-only and a narrowed or non-owner session is refused.
A subject whose latest fix is stale is reported as last-known, never "here now".
Returns person labels, place names, and times — never a coordinate.
