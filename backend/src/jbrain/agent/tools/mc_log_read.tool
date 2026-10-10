---
name: mc_log_read
version: 1
permission: web
params:
  type: object
  properties:
    goal:
      type: integer
      description: Only entries toward this goal number.
    limit:
      type: integer
      description: How many recent entries (1-100, default 20).
---
Read this chat's player's progress log, oldest first, with who wrote each entry.
