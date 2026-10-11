---
name: mc_replicator_program
version: 1
permission: web
params:
  type: object
  properties:
    target:
      type: string
      description: What the Replicator should make, as the item's name, e.g. "iron", "diamond" or "oak_log".
  required: [target]
---
Program the Replicator the player asking is holding, in game only: "Dave, program this
replicator for iron." They must hold a Replicator (blank or already programmed) and carry
8 full stacks (512) of the target, which you take as its pattern; they're gone, and it
becomes, say, an "Iron Replicator". Only things that stack to 64 can be a pattern.
Reprogramming takes 8 new stacks and the old pattern is lost. The server checks
everything and says if anything is missing: then nothing was taken.
