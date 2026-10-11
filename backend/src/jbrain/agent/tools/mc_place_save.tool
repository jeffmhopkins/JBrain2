---
name: mc_place_save
version: 1
permission: web
params:
  type: object
  properties:
    name:
      type: string
      description: The place's name, up to 40 characters, e.g. "home" or "tree farm". Saving a name the player already has moves that place.
    x:
      type: integer
      description: The place's X; leave x, y and z out to save where the player is.
    y:
      type: integer
      description: The place's Y.
    z:
      type: integer
      description: The place's Z.
    dimension:
      type: string
      enum: [overworld, nether, the_end]
      description: The place's dimension, with coordinates (default where the player is).
  required: [name]
---
Save a named place for this chat's player in the loaded world: "mark this as tree farm",
"remember my base is at 100 64 -20". With no coordinates it saves where the player is
standing when they ask in game, else where they were last seen. Names ignore case, and
saving one again moves it. mc_set_waypoint's `place` points their Tricorder at it.
