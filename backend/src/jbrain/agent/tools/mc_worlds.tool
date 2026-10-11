---
name: mc_worlds
version: 1
permission: web
params:
  type: object
  properties:
    slot:
      type: string
      description: A slot like slot2 to read that world's game rules; leave it out to list every world slot.
  required: []
---
The server's world slots — each world's name, seed, game mode, difficulty, cheats, when
it was last played and which one is loaded — or, given a slot, that world's game rules.
