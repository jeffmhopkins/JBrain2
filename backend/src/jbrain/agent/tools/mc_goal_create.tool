---
name: mc_goal_create
version: 1
permission: web
side_effecting: true
params:
  type: object
  properties:
    title:
      type: string
      description: The goal, short — e.g. "Beacon at the base" or "20 obsidian for the portal".
    notes:
      type: string
      description: Optional detail — what it needs, where, by when.
  required: [title]
---
Add a goal for this chat's player.
