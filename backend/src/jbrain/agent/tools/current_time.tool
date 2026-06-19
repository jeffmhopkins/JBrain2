---
name: current_time
version: 1
permission: read
params:
  type: object
  properties:
    timezone:
      type: string
      description: Optional IANA timezone name (e.g. "America/New_York", "Asia/Tokyo") to read the time in. Defaults to the owner's own timezone.
  required: []
---
Return the current date, the day of the week, and the local time. The current date
and time are also given to you as ambient context at the start of each turn, so call
this only when you need a fresh reading mid-conversation or the time in a SPECIFIC
timezone — pass an IANA name like "Asia/Tokyo" to convert. With no timezone it
answers in the owner's own zone (UTC if unknown). This reads a clock only — never
the owner's notes, location, or any other data.
