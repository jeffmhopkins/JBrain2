---
name: device_status
version: 1
permission: read
domains: [location]
params:
  type: object
  properties: {}
---
List the tracked devices with read-only status flags: when each was last heard
from (freshness: fresh / stale / no-fix) and its last reported battery (tone: ok /
low / unknown), labeled by the person each device is linked to. OWNER-ONLY:
location is owner-only and a narrowed or non-owner session is refused. The flags
are computed on read, never stored. Returns names, times, and enum tones — never a
coordinate.
