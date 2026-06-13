---
name: read_appointment
version: 1
permission: read
params:
  type: object
  properties:
    appointment_id:
      type: string
      description: The id of the appointment to read (from read_appointments).
  required: [appointment_id]
---
Read one of the owner's appointments by id, in full — its time, status, location,
recurrence, and attendees. Returns a not-in-scope message if no such appointment
is visible in this session. Read-only: an appointment is projected from the
owner's notes (the sole source of truth), so changes are proposed, not written
here.
