---
name: mc_place_remove
version: 1
permission: web
params:
  type: object
  properties:
    name:
      type: string
      description: The saved place to forget.
  required: [name]
---
Forget one of this chat's player's saved places in the loaded world.
