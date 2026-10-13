---
name: mc_gates_beacons
version: 1
permission: web
params:
  type: object
  properties:
    player:
      type: string
      description: In the app only, one player's gamertag to narrow to; leave it out for everyone's. In game it is always the player asking.
    slot:
      type: integer
      description: In the app only, the world slot, 1-5. Leave it out for the loaded world.
  required: []
---
Wormhole gates and No-Spawn Beacons in the world, from the game's own records: where each
gate is, whether it's open and where it leads, where each beacon is and the 48 × 48 square
it protects, and which of the player's saved places a beacon covers. For "where's my
gate?", "where does gate 2 go?", "is my base protected?". In game it is only the asker's
own. It works for a world that isn't loaded, as of the game's last report.
