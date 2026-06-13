---
name: manage_appointment
version: 1
permission: sensitive
params:
  type: object
  properties:
    action:
      type: string
      enum: [create, reschedule, cancel]
      description: Whether to add a new appointment, move an existing one, or cancel one.
    title:
      type: string
      description: What the appointment is, e.g. "dentist with Dr. Nguyen". Required to create; for reschedule/cancel you may pass an appointment_id instead.
    when:
      type: string
      description: The date and time, in plain words, e.g. "next Friday at 2pm" or "2026-07-01 14:00". Required for create and reschedule.
    location:
      type: string
      description: Where it is (optional).
    appointment_id:
      type: string
      description: The existing appointment to reschedule or cancel (from read_appointments).
    domain:
      type: string
      description: Which domain it belongs to — general, health, finance, or location. Defaults to this session's scope or the existing appointment's domain.
  required: [action]
---
Propose adding, rescheduling, or cancelling an appointment for the owner to
approve. This NEVER changes the calendar directly — appointments are derived from
the owner's notes, so you have no privileged write path. It stages a Proposal the
owner reviews; on approval it re-enters as a normal, dated note through the same
pipeline any note goes through, and the appointment then appears (or moves, or is
marked cancelled). To move or cancel an existing appointment, pass its
appointment_id (from read_appointments) so the change lands on the same
appointment. Tell the owner you've staged it for review.
