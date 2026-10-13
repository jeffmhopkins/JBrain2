---
name: mc_find_pet
version: 1
permission: web
params:
  type: object
  properties:
    kind:
      type: string
      description: The animal, e.g. wolf (dog), cat, parrot, horse, donkey, llama.
    name:
      type: string
      description: A name tag's name, to find any mob called that (a pet, a pig, a villager), e.g. "Rex".
    gamertag:
      type: string
      description: Whose pets. In game, leave it out for the player asking; any player's can be named.
    anyone:
      type: boolean
      description: Every tamed pet, whoever owns it.
    dimension:
      type: string
      enum: [overworld, nether, the_end]
      description: Which dimension to measure from (default the player's own); pets in other dimensions are listed after.
    x:
      type: integer
      description: Search around this X. Leave x and z out to search around the player.
    z:
      type: integer
      description: Search around this Z.
    limit:
      type: integer
      description: How many to list (default 5, up to 20).
  required: []
---
Tamed pets and named mobs in the saved world, nearest first: "where's my dog?", "where
did Clover go?", "where are Mira's cats?". With no gamertag in game it's the asking
player's own pets; anyone's can be found by their gamertag, `anyone` for every tamed pet,
and `name` finds any mob with that name tag, the player's own first. Each line gives x,
y and z, the way from the player, and whether it's sitting; names and owners are
player-written. A pet wanders unless it's sitting: it's where the world last saved it.
In game, within 48 blocks of the player is free; farther is one of their deep scans,
their own pets included, and the same search again from about the same place is free.
Point their Tricorder at it with mc_set_waypoint.
