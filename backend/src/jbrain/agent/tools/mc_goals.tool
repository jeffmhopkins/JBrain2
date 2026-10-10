---
name: mc_goals
version: 1
permission: web
params:
  type: object
  properties:
    status:
      type: string
      enum: [open, done, abandoned, all]
      description: Which goals to list (default open).
---
This chat's player's goals, numbered. The numbers never change, so use them with
mc_goal_update and mc_log.
