---
name: read_entity
version: 3
permission: read
params:
  type: object
  properties:
    entity_id:
      type: string
      description: The id of the entity to read (from a search result, relate, or another entity's edges).
  required: [entity_id]
---
Read one of the owner's entities by its id and get the structured GRAPH view: its
type, names and aliases, its current facts as edges (predicate → value), which
other entities point at it, and how many notes mention it. Use this for the
structured view of who or what something is.

To read the OWNER'S OWN entity ("Me" — the centre of the graph, where their own
birthday, name, age, email, and the like live), pass entity_id "me". That is the
one-call way to answer a first-person attribute question; you don't need to look
its id up first.

The edges are how you traverse relationships. A relationship edge (spouse,
employer, parent, manages, …) names another entity and prints its id — read THAT
entity to follow the relationship one hop further. This is how you answer "my
wife's name" by hand: read the owner ("Me"), find the spouse edge, read the
entity it points at, and report its name (the relate tool does this in one step
from the owner). Inbound edges ("referenced by …") let you go the other
direction. Returns nothing if no such entity is in this session's scope.
