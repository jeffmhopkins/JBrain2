---
name: mc_set_waypoint
version: 3
permission: web
params:
  type: object
  properties:
    place:
      type: string
      description: A saved place's name (mc_places) to point at instead of coordinates. "home" with no saved home is the player's bed when they ask in game.
    x:
      type: integer
      description: The destination's X.
    z:
      type: integer
      description: The destination's Z.
    y:
      type: integer
      description: The destination's Y, when depth matters (a diamond, a buried chest); shown on the Tricorder.
    dimension:
      type: string
      enum: [overworld, nether, the_end]
      description: The destination's dimension (default overworld, or where the player is in game).
    label:
      type: string
      description: What the Tricorder shows, up to 40 characters, e.g. "Diamonds" or "Mansion" (a place's own name by default).
    gamertag:
      type: string
      description: Whose Tricorder to set; leave it out for this chat's player.
    clear:
      type: boolean
      description: Clear the waypoint instead, pointing the Tricorder back at spawn.
  required: []
---
Point a player's Tricorder at a destination: coordinates, or one of their saved places.
While they hold it, its screen line shows an arrow toward the place, its label and how far
it is (and the y, if given), or which dimension it's in when they're elsewhere. Each
player has their own waypoint, kept with the world, and it can be set while they're
offline. The Tricorder comes with the box's own jbrain pack, in every world it loads.
