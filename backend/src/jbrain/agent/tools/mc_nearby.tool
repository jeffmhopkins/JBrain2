---
name: mc_nearby
version: 1
permission: web
params:
  type: object
  properties:
    x:
      type: integer
      description: Search around this X. Leave x and z out to use this chat's player's last known position.
    z:
      type: integer
      description: Search around this Z.
    dimension:
      type: string
      enum: [overworld, nether, the_end]
      description: Which dimension (default overworld).
    radius:
      type: integer
      description: Only list structures within this many blocks (default 2000).
  required: []
---
The structures around a spot: the nearest of every kind the dimension has (villages,
mansions, monuments, outposts, ancient cities, trial chambers, temples, shipwrecks,
treasure, portals, strongholds; fortresses and bastions in the Nether; end cities), with
their coordinates and distance. Asked of the running server's own world generator, so it
is exact and includes places nobody has explored. Needs the server running.
