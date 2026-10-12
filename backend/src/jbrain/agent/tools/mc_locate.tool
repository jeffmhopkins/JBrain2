---
name: mc_locate
version: 4
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
      description: The Bedrock id, e.g. mansion, village, ancient_city, trial_chambers for structures, or cherry_grove, mushroom_island, deep_dark for biomes (Bedrock ids, so badlands is mesa).
    x:
      type: integer
      description: Search from this X. Leave x and z out to search from this chat's player's last known position.
    z:
      type: integer
      description: Search from this Z.
    dimension:
      type: string
      enum: [overworld, nether, the_end]
      description: Which dimension to search (default the player's own in game, else the Overworld). Nether structures are fortress, bastion_remnant and ruined_portal; the End's is end_city.
  required: [kind, id]
---
Find the nearest structure or biome of a kind, in any dimension, using the server's own
world generator — it answers even for places nobody has explored. Gives the coordinates
and the distance from where it searched.
In game this is one of the asking player's deep scans (two per Minecraft day); the
same search again from about the same place is free, and the answer says how many
are left.
