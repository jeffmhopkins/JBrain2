---
name: mc_log
version: 1
permission: web
side_effecting: true
params:
  type: object
  properties:
    text:
      type: string
      description: The progress entry, e.g. "got 12 obsidian".
    goal:
      type: integer
      description: Optional goal number from mc_goals this progress is toward.
    by_dave:
      type: boolean
      description: True only when this entry is your own summary that the player asked for, rather than their words.
  required: [text]
---
Add an entry to this chat's player's progress log — the journal of how they're getting
on toward their goals.
