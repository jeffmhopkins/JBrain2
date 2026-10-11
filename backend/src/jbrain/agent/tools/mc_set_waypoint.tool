---
name: mc_set_waypoint
version: 1
permission: web
params:
  type: object
  properties:
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
      description: The destination's dimension (default overworld).
    label:
      type: string
      description: What the Tricorder shows, up to 40 characters, e.g. "Diamonds" or "Mansion".
    gamertag:
      type: string
      description: Whose Tricorder to set; leave it out for this chat's player.
    clear:
      type: boolean
      description: Clear the waypoint instead, pointing the Tricorder back at spawn.
  required: []
---
Point a player's Tricorder at a destination. While they hold it, its screen line shows an
arrow toward the place, its label and how far it is (and the y, if given), or which
dimension it's in when they're elsewhere. Each player has their own waypoint, kept with
the world, and it can be set while they're offline. Needs the Tricorder add-on (the
home-test kit) in the loaded world.
