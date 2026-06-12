---
name: read_entity
version: 1
permission: read
params:
  type: object
  properties:
    entity_id:
      type: string
      description: The id of the entity to read (from a search result or another entity's edges).
  required: [entity_id]
---
Read one of the owner's entities — a person, organization, place, event, medical
condition, drug, and so on — by its id. Returns the entity's type, its names and
aliases, its current facts as edges (predicate → value), which other entities point
at it, and how many notes mention it. Use this for the structured, graph view of
who or what something is. Returns nothing if no such entity is in this session's
scope.
