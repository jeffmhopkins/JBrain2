---
name: mc_find_villager
version: 1
permission: web
params:
  type: object
  properties:
    profession:
      type: string
      description: A villager job, e.g. librarian, cleric, armorer, toolsmith, weaponsmith, farmer, fletcher, cartographer, mason, nitwit, wandering_trader.
    sells:
      type: string
      description: A Bedrock item id the villager sells, e.g. enchanted_book, diamond_pickaxe, bell.
    enchantment:
      type: string
      description: An enchantment it sells (on a book or an item), e.g. mending, unbreaking, silk_touch, efficiency, protection, looting.
    x:
      type: integer
      description: Search around this X. Leave x and z out to search around the player.
    z:
      type: integer
      description: Search around this Z.
    radius:
      type: integer
      description: How far to look, in blocks. Leave it out to search the whole dimension.
    dimension:
      type: string
      enum: [overworld, nether, the_end]
      description: Which dimension (default the player's own, else overworld).
    limit:
      type: integer
      description: How many villagers to list (default 5, up to 20).
  required: []
---
Villagers in the saved world, nearest first, with their job, level and what they sell
right now: "who sells Mending?", "where's a librarian?", "find me a cleric". Each line
gives x, y and z and the way from the player; a trade says its price ("an enchanted book
(Mending I) for 12 emerald and 1 book") and whether it's used up until the villager
restocks. Only the trades a villager offers now are told, never its locked higher
levels. When the answer says "as of N minutes ago", say so. In game, what's within 48
blocks of the player is the Tricorder's short-range scan, free; farther is one of their
deep scans (two per Minecraft day), and the same search again from about the same place
is free. Point their Tricorder at the one they want with mc_set_waypoint.
