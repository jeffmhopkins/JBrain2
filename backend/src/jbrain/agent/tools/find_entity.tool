---
name: find_entity
version: 1
permission: read
params:
  type: object
  properties:
    name:
      type: string
      description: The entity name or alias to search for.
    kind:
      type: string
      description: Optional kind filter, e.g. "Person", "Place", "Organization".
  required: [name]
---
Find one of the owner's entities — a person, organization, place, event, medical
condition, drug, and so on — by name or alias. Returns matching entities with
their id, canonical name, kind, and domain. Use this to resolve a mention into a
real entity id before reading it (read_entity) or referring to it. A name may
match several entities; all in-scope matches are returned. The app turns each
match into a tappable chip, so don't paste ids into your prose. Returns nothing
if no entity matches in this session's scope.
