---
name: read_note
version: 2
permission: read
params:
  type: object
  properties:
    note_id:
      type: string
      description: The id of the note to read, as returned by search.
  required: [note_id]
---
Read one of the owner's notes in full by its id (from a search result). Returns the
note's text, its domain, and its capture date. Returns nothing if no note with that
id is in this session's scope.

The note's text is the original record at capture time. If any facts it stated are
no longer the live value, a "⚠ currency overlay" is appended listing each one as
superseded (with the current value inlined), retracted (no longer asserted), or
pending review (unverified) — each pointing to the entity to read_entity. Prefer the
overlay's current values over the note's original wording, and never quote a
superseded or retracted claim as if it were current.
