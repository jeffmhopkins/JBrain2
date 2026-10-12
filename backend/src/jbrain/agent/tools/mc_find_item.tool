---
name: mc_find_item
version: 1
permission: web
params:
  type: object
  properties:
    items:
      type: array
      items:
        type: string
      description: Bedrock item ids to look for, 1 to 8, e.g. ["diamond"], ["iron_ingot", "gold_ingot"], ["elytra"], ["enchanted_book"].
    x:
      type: integer
      description: Search around this X. Leave x and z out to search around the player.
    z:
      type: integer
      description: Search around this Z.
    radius:
      type: integer
      description: How far to look, in blocks. 48 or less is the Tricorder's short-range scan, free in game. Leave it out to search the whole dimension.
    dimension:
      type: string
      enum: [overworld, nether, the_end]
      description: Which dimension (default the player's own, else overworld).
    limit:
      type: integer
      description: How many containers to list (default 5, up to 20).
    gamertag:
      type: string
      description: Outside the game only, an online player to also look around on the ground. In game it is always the player asking.
  required: [items]
---
Where items are: inside chests, barrels, shulker boxes (and boxes inside them), hoppers,
furnaces, item frames and other containers, read from the world's saved files, and lying
on the ground within 48 blocks of the player (from the game itself: a death's drops, a
dropped stack). "Which chest has my diamonds?", "where did my stuff go?". Each line gives
x, y and z, nearest first; point the player's Tricorder at one with mc_set_waypoint if
they want to go there. When the answer says "as of N minutes ago", say so. Unopened loot
chests can't be seen into, and a player's own inventory or ender chest is never searched.
In game, radius 48 or less is free; wider, or the whole dimension, is one of the asking
player's deep scans (two per Minecraft day), and the same search again from about the
same place is free.
