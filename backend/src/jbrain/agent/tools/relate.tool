---
name: relate
version: 2
permission: read
params:
  type: object
  properties:
    relationship:
      type: string
      description: >-
        The relationship or role to follow — e.g. "wife", "husband", "mom",
        "boss", "doctor", "landlord", or a stored predicate like "spouse" or
        "employer". A relationship word, never a person's name.
    from:
      type: string
      description: >-
        Optional entity id (from find_entity or read_entity) to start from. Omit
        for the owner — use the default for first-person questions like
        "my wife" or "my manager".
  required: [relationship]
---
Follow one of the owner's relationships to the entity on the other side —
answering "who is my <relationship>" without searching by a name you don't have.
Relationship words ("wife", "boss", "mom", "my doctor") are NOT entity names;
this is the tool for them. By default it starts from the owner (the "Me" entity
at the center of the graph), so "what's my wife's name" is one call:
relate(relationship="wife") returns the spouse entity — then read_entity it for
the name, or just report the name it returns. Pass `from` to hop off someone
else ("her doctor": find_entity that person, then relate from their id). Returns
the matching entities (predicate → entity, with ids to read_entity), as tappable
chips; nothing if the owner has no such relationship in this session's scope. Use
find_entity instead when the user names the person directly, and neighborhood
instead when you want everything AROUND an entity (all connections at every hop,
plus the notes linking them) rather than the other end of one named relationship.
