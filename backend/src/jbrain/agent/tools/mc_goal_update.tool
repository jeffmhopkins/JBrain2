---
name: mc_goal_update
version: 1
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
  required: [goal]
---
Finish, abandon, reopen or rename one of this chat's player's goals.
