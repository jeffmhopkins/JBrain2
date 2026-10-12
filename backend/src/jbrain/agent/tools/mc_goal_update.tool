---
name: mc_goal_update
version: 2
permission: web
side_effecting: true
params:
  type: object
  properties:
    goal:
      type: integer
      description: The goal's number from mc_goals.
    status:
      type: string
      enum: [open, done, abandoned]
      description: Mark it done, abandoned, or open again.
    title:
      type: string
      description: A new title.
    item:
      type: string
      description: The Bedrock item id the goal collects, e.g. "iron_block".
    count:
      type: integer
      description: How many of the item they need, 1 to 100000 ("I need 30 now, not 23").
  required: [goal]
---
Finish, abandon, reopen or rename one of this chat's player's goals, or change the item
and how many it counts (a goal that counts nothing yet needs both).
