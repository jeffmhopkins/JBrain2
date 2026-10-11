---
name: mc_what_is_at
version: 2
permission: web
params:
  type: object
  properties:
    x:
      type: integer
      description: The block X. Leave x and z out to use this chat's player's last known position.
    z:
      type: integer
      description: The block Z.
    dimension:
      type: string
      enum: [overworld, nether, the_end]
      description: Which dimension (default overworld).
  required: []
---
What is at one spot: its biome and the height of the ground there. Ground someone has
explored is read from the world itself; ground nobody has visited, in any dimension, comes
from the satellite's survey, predicted from the seed (the biome is almost always right, the
height within a few blocks). Open void in the End reads as unknown. One call answers "what
biome is at x, z" — don't search biome by biome with mc_locate for that.
