---
name: mc_wormhole_aim
version: 1
permission: web
params:
  type: object
  properties:
    place:
      type: string
      description: One of this player's own saved places (mc_places) for the gate to lead to, e.g. "tree farm".
  required: [place]
---
Open this player's wormhole gate to one of their own saved places, or re-aim it at another.
Use it only when they ask for that. They must be standing within 4 blocks of a gate they
planted, and the place must be in a dimension they have been to. The answer says what
changed and what it cost (the first opening is free; a re-aim burns one Power Pack, only
once the gate has changed), or why nothing did.
