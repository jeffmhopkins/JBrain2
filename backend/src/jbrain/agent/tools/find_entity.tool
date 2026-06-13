---
name: find_entity
version: 2
permission: read
params:
  type: object
  properties:
    name:
      type: string
      description: >-
        An entity's actual name or alias — a proper name like "Celine" or
        "Acme Corp", never a relationship word like "wife" or "boss".
    kind:
      type: string
      description: Optional kind filter, e.g. "Person", "Place", "Organization".
  required: [name]
---
Find one of the owner's entities by its NAME or ALIAS — a person, organization,
place, event, medical condition, drug, and so on (e.g. "Celine", "Acme Corp",
"Dr. Patel"). Use it to turn a proper name into an entity id you can then read
(read_entity) or refer to. Returns the matching entities with their id, canonical
name, kind, and domain, as tappable chips — so don't paste ids into your prose.

Pass only an actual name here — never a relationship or role word. "Wife",
"boss", "mom", "my doctor", "the landlord" are relationships, not entity names;
searching for them finds nothing. For those, use relate (it starts from the
owner) — or, by hand, find the entity the relationship hangs off, read it, and
follow the matching edge to the entity on the other side. A name may match
several entities; all in-scope matches are returned. Returns nothing if no entity
matches in this session's scope.
