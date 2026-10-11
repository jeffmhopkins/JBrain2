---
name: mc_players
version: 2
permission: web
params:
  type: object
  properties:
    gamertag:
      type: string
      description: One player, for "where is Mira?". Leave it out to list everyone.
---
Everyone who has played on the server: total play time, number of sessions, when they
were last seen, and who is online now. With a gamertag, where that one player is: if
they're online, where they are now (and how far and which way from the player asking,
in game); if not, where they were last seen in the loaded world.
