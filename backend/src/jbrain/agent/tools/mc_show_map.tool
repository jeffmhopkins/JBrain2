---
name: mc_show_map
version: 3
permission: web
params:
  type: object
  properties:
    focus:
      type: string
      description: What the card is about — death, player, place, structure, spot, gate, beacon or scan. death with no x and z is this chat's player's latest death; player with none is where they were last seen; place with none is the saved place named in label; scan is a player's live Tricorder scan (the gamertag in label, else this chat's player), drawn live with the square it searches and the block it found. structure, spot, gate and beacon need x and z (a gate's or beacon's from mc_gates_beacons).
    label:
      type: string
      description: A short label for the marked spot, up to 40 characters, e.g. "Woodland mansion". For place with no coordinates, the saved place's name; for scan, whose (a gamertag).
    x:
      type: integer
      description: The block X to mark and centre on. Leave x and z out to use the focus above, or this chat's player's last position.
    z:
      type: integer
      description: The block Z.
    dimension:
      type: string
      enum: [overworld, nether, the_end]
      description: Which dimension (default overworld). A death, player or place found from the log brings its own.
    slot:
      type: integer
      description: The world slot, 1-5. Leave it out for the loaded world.
    zoom:
      type: integer
      description: 0 is one pixel per block, 4 one per chunk (widest); -1 to -3 zoom in to 8 pixels per block. Leave it out for a sensible default.
    show:
      type: array
      items:
        type: string
      description: Which overlays to draw, from trails, deaths, structures, players, beacons and gates. Default all but players.
  required: []
---
Show a live piece of the Minecraft map in the chat: the box's biome map (explored ground in
colour over the satellite's survey) with the marked spot, the player's trail, deaths, the
structures the server has located, the No-Spawn Beacons' protected squares and where each
open wormhole gate leads, plus a scale bar. The owner can pinch it and tap it to
open the full map. Use it when a place, a death or a structure is easier to see than to
read: "where did I die?", "where's the village?", "show me my base". Still give the
coordinates in words. Only in the app — never in game.
