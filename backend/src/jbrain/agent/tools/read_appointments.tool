---
name: read_appointments
version: 1
permission: read
params:
  type: object
  properties:
    include_past:
      type: boolean
      description: Include appointments that have already started. Defaults to false (upcoming only).
    include_cancelled:
      type: boolean
      description: Include cancelled appointments. Defaults to false.
  required: []
---
List the owner's appointments — title, time, domain, and whether each repeats or
is cancelled — soonest first. By default it returns only upcoming appointments;
set include_past to look back. Each line includes the appointment id — use it
with read_appointment to see one in full. Read-only: appointments are projected
from the owner's notes, so to add or move one you propose a change, you don't
write here.
