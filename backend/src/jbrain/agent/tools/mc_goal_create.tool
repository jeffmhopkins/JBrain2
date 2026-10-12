---
name: mc_goal_create
version: 2
permission: web
side_effecting: true
params:
  type: object
  properties:
    title:
      type: string
      description: The goal, short — e.g. "Beacon at the base" or "Get 23 iron blocks".
    notes:
      type: string
      description: Optional detail — what it needs, where, by when.
    item:
      type: string
      description: For a goal of collecting something, the Bedrock item id, e.g. "iron_block", "oak_log", "diamond". Give count with it.
    count:
      type: integer
      description: How many of item they need, 1 to 100000.
  required: [title]
---
Add a goal for this chat's player. "Remind me I need 23 iron blocks" is a goal with item
"iron_block" and count 23, so mc_goal_progress can count how many they have.
