---
name: mc_locate
version: 1
permission: web
params:
  type: object
  properties:
    kind:
      type: string
      enum: [structure, biome]
      description: Whether to find a structure or a biome.
    id:
      type: string
      description: The Bedrock id, e.g. mansion, village, ancient_city, trial_chambers for structures, or cherry_grove, mushroom_fields, deep_dark for biomes.
    x:
      type: integer
      description: Search from this X. Leave x and z out to search from this chat's player's last known position.
    z:
      type: integer
      description: Search from this Z.
  required: [kind, id]
---
Find the nearest structure or biome of a kind in the Overworld, using the server's own
world generator — it answers even for places nobody has explored. Gives the coordinates
and the distance from where it searched. Nether and End structures can't be searched
this way yet.
