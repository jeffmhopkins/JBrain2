---
name: mc_player
version: 1
permission: web
side_effecting: true
params:
  type: object
  properties:
    gamertag:
      type: string
      description: The Minecraft player this chat should be about. Leave it out to see who the chat is about now.
---
Show or switch which Minecraft player this chat is about. Goals, the progress log,
memory and play history are always that one player's. Switch only when asked ("switch
to Mira", "this is about my brother").
