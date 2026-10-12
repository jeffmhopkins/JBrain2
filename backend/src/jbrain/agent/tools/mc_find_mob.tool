---
name: mc_find_mob
version: 1
permission: web
params:
  type: object
  properties:
    mob:
      type: string
      description: The Bedrock mob id, e.g. cow, sheep, pig, horse, wolf, cat, villager, iron_golem, zombie, enderman, piglin.
    gamertag:
      type: string
      description: Whose surroundings to search (online players only). In game it is always the player asking; leave it out for this chat's player.
    max_distance:
      type: integer
      description: How far to look, in blocks (default and most 256).
  required: [mob]
---
The nearest mob of a kind around a player, with its x, y and z, asked of the game itself:
"find me the nearest cow". Mobs exist only in the chunks around online players, so this
looks nearby (up to 256 blocks), the player must be online, and a mob moves — the answer
is where it is now. It isn't a deep scan and costs none. After finding one, point the
player's Tricorder at it with mc_set_waypoint if they want to go there.
