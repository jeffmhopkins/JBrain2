---
name: mc_play_history
version: 1
permission: web
params:
  type: object
  properties:
    days:
      type: integer
      description: How many days back to look (1-90, default 14).
---
This chat's player's play sessions — when they played and for how long — over the last
few days.
