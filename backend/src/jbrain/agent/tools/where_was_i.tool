---
name: where_was_i
version: 1
permission: read
domains: [location]
params:
  type: object
  properties: {}
---
Report where the owner's own linked device currently is — the owner's last known
place plus the freshness of the last fix. OWNER-ONLY: location is owner-only and a
narrowed or non-owner session is refused. Requires the owner's device to be linked;
otherwise it reports that no device is linked. Returns the place name and when it
was last seen, and FLAGS a stale fix as a last-known position — never a coordinate,
never "here now" for an old fix.
