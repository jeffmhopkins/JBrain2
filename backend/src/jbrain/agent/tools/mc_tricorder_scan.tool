---
name: mc_tricorder_scan
version: 2
permission: web
params:
  type: object
  properties:
    block:
      type: string
      description: The Bedrock block id to scan for, e.g. diamond_ore (its deepslate twin comes along), iron_ore, ancient_debris, oak_log. Any block.
    stop:
      type: boolean
      description: End the scan instead; the Tricorder points at their waypoint again (or spawn).
    status:
      type: boolean
      description: Change nothing; say whether their Tricorder is scanning, for what, how far it reaches, and what its last scan found.
    gamertag:
      type: string
      description: Whose Tricorder; leave it out for this chat's player. In game it is always the player asking.
  required: []
---
Set a player's Tricorder to scan for a block: "set my Tricorder to scan for diamonds".
Every 10 seconds it finds the nearest one within 16 blocks of them and, while they hold
it, the arrow and distance point at it; with none that close it reads "Scanning: diamond ·
none nearby". It keeps scanning, across sessions, until they ask you to stop (`stop`) or
you set a new waypoint, which replaces it. It's the Tricorder's own short-range sense:
free, never a deep scan. An unknown block id is refused: try its Bedrock id.
`status` reads it instead: "is my Tricorder still scanning?", "what did it find?" — the
block's distance and direction across (as the held Tricorder says it), how far below or
above them it is, and the heights the scan reaches from where they stand. An offline
player's scan is kept and picks up again when they're back on.
