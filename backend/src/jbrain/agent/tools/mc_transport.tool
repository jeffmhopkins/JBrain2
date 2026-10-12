---
name: mc_transport
version: 1
permission: web
params:
  type: object
  properties:
    place:
      type: string
      description: The name of one of the asking player's own saved places (mc_places), like "home" or "tree farm".
  required: [place]
---
Transport the player asking in game to one of their own saved places, for one Power Pack.
Only in game, only the asker, only a place they saved themselves: never a bed, a structure,
coordinates or another player. They need their own Tricorder and a Power Pack on them, and
the place must be in a dimension they've been to. A Yes / No form opens on their screen
naming the place, how far it is, the dimension and the cost; nothing happens or is spent
unless they choose Yes and the landing is clear. The result says how it ended. Tell them in
a sentence; on "no Power Pack", point their Tricorder there instead (mc_set_waypoint).
