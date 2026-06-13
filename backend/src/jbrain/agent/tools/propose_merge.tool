---
name: propose_merge
version: 1
permission: sensitive
params:
  type: object
  properties:
    entity_a:
      type: string
      description: The id of one entity to merge (from find_entity).
    entity_b:
      type: string
      description: The id of the other entity — the duplicate of entity_a (from find_entity).
    reason:
      type: string
      description: Optional short note on why they're the same thing.
  required: [entity_a, entity_b]
---
Propose merging two entities that are the same real thing (a duplicate) into one.
Use THIS — never propose_correction — when the owner has two entity records for one
person, place, organization, product, and so on, and wants them combined. Pass the
two entity ids from find_entity (not names, not prose).

This NEVER writes: it stages a Proposal the owner approves. On approval the
more-anchored identity survives and the other's mentions and facts repoint onto it,
so nothing is lost — and a permanent "kept separate" decision blocks re-proposing
the same pair. Don't paste ids into your reply; the merge rides structurally on the
proposal, and you tell the owner you've staged it for review.
