---
name: read_note
version: 1
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
