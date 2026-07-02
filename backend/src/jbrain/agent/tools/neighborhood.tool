---
name: neighborhood
version: 1
permission: read
params:
  type: object
  properties:
    anchor:
      type: string
      description: >-
        The entity id to center on (from find_entity, read_entity, or relate).
        Omit — or pass "me" — for the owner's own entity.
    hops:
      type: integer
      minimum: 1
      maximum: 3
      description: How far out to walk, 1-3. Default 2.
    kinds:
      type: string
      enum: [relationships, co-mentions, both]
      description: >-
        Which connections to walk — "relationships" (stored fact edges like
        spouse or worksFor), "co-mentions" (entities appearing in the same
        note), or "both" (default).
    limit:
      type: integer
      description: Maximum entities to return (default 75).
  required: []
---
Survey the WHOLE vicinity around one entity: every entity within 1-3 hops —
across relationship edges AND shared-note co-mentions — plus the notes that
connect them. One call answers "who and what is around X", "pull everything
within 2 connections of this person", or "which notes tie X's circle together":
each neighbor comes with its hop distance, the path that reached it, and an id
to read_entity; each connecting note comes with its id to read_note. Use this
when you want the surroundings without knowing what you're looking for. It is
NOT the tool for following one named relationship — "who is my wife" is
relate(relationship="wife"), which answers a relationship word with the entity
on the other side; neighborhood maps everything around an anchor regardless of
how it is connected. Defaults to the owner ("Me") when anchor is omitted.
Returns nothing if the anchor entity is not in this session's scope.
