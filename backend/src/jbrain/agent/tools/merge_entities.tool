---
name: merge_entities
version: 1
permission: sensitive
mutating: true
side_effecting: true
cost_class: standard
params:
  type: object
  properties:
    entity_a:
      type: string
      description: >-
        One of the two — its id from find_entity, or its exact name as find_entity
        printed it.
    entity_b:
      type: string
      description: The other one, the same way.
    reason:
      type: string
      description: >-
        One sentence saying what makes them the same thing — "Jeff says the Dana in this
        note is the Dana Whitfield already on file." An empty string if he simply said so.
  required: [entity_a, entity_b, reason]
examples:
  - entity_a: 0f7a1c4e-2b3d-4a5f-8c9d-0e1f2a3b4c5d
    entity_b: 7c1b9a30-55de-4f2a-9a11-2b6c0d8e4f31
    reason: Jeff says the Dana in this note is the Dana Whitfield already on file.
---
Two entities turn out to be one thing. Stage the fold for Jeff to approve.

This does NOT merge them. Folding two entities together moves every mention and every
fact from one onto the other across all of his domains at once, which is a write this
conversation is not permitted to make — it can only put the question in front of him.
So nothing changes when you call this. Tell him it is waiting on him; never say they
have been merged.

You do not choose which one survives, and there is no argument to say so. When he
approves, the more-anchored identity is kept and the other's mentions and facts repoint
onto it, so nothing is lost either way.

Use it when he tells you two records are the same person, place or thing, or when he
confirms it after you asked. Not on a hunch about two similar names — a wrong fold is
his to undo, and the point of asking him is that he knows and you do not. If he has
already said they are different, this refuses and says so.
