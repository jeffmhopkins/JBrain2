---
name: mc_backups
version: 1
permission: web
params:
  type: object
  properties:
    slot:
      type: string
      description: Only this world slot's backups; leave it out for all.
  required: []
---
The server's backups: each file name, its world, when it was made, its size, and whether
it is pinned (kept forever) or automatic.
