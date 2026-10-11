---
name: mc_place_save
version: 2
permission: web
params:
  type: object
  properties:
    name:
      type: string
      description: The place's name, up to 40 characters, e.g. "home" or "tree farm". Saving a name the player already has moves that place.
  required: [name]
---
Save a named place for this chat's player in the loaded world, at the spot where they are
right now: "save this place as mine entrance" ("place a transport beacon here" means the
same). It takes no coordinates, so a place is always
somewhere the player has stood. Asked in game, it saves where they're standing. From the
app, it saves where they are now, and only while they're online, from a position under a
minute old. Names ignore case, and saving one again moves it. mc_set_waypoint's `place`
points their Tricorder at it.
