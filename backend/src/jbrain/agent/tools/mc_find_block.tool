---
name: mc_find_block
version: 1
permission: web
params:
  type: object
  properties:
    blocks:
      type: array
      items:
        type: string
      description: Bedrock block ids to look for, e.g. ["diamond_ore"] (an ore's deepslate twin is included), ["ancient_debris"], ["mob_spawner"], ["chest"].
    x:
      type: integer
      description: Search around this X. Leave x and z out to search around this chat's player's last known position.
    z:
      type: integer
      description: Search around this Z.
    dimension:
      type: string
      enum: [overworld, nether, the_end]
      description: Which dimension (default overworld).
    limit:
      type: integer
      description: How many to return (default 5, up to 20).
    radius:
      type: integer
      description: How far to look, in blocks (default 2048).
  required: [blocks]
---
The nearest blocks of the given kinds, with their exact x, y and z, read from the world's
saved chunks — "point me to the nearest diamond". Only explored ground can be searched:
ores and other blocks don't exist until the game generates a chunk. If none of the names is
in explored ground, it answers with block ids that are, so you can try again.
