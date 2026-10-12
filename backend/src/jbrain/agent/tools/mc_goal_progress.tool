---
name: mc_goal_progress
version: 1
permission: web
params:
  type: object
  properties:
    goal:
      type: integer
      description: The goal's number from mc_goals. Leave it out when only one open goal counts an item.
    gamertag:
      type: string
      description: Outside the game only, the chat's player's gamertag, to also count what they carry while online. In game it is always the player asking.
---
How far along a goal that counts an item is: "how many iron blocks have I got, how many
more do I need?". Adds what the player carries (inventory, armor, off-hand; not inside
shulker boxes) to what's in the chests and other containers within 48 blocks of each of
their saved places and of where they stand, each container once, and says where they are
("17 of 23 — 6 to go", "12 in chests near home, 5 on you"). Free, except in game when a
saved place is beyond 48 blocks of the player: counting those is one of their deep scans
(the same check again from about the same place is free). When the goal is reached it
says so: ask before marking it done. For anywhere else in the world, use mc_find_item.
