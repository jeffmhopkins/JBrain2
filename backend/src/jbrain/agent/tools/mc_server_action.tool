---
name: mc_server_action
version: 1
permission: web
params:
  type: object
  properties:
    action:
      type: string
      enum: [start, stop, restart, backup, load_world, create_world, update_world, reset_world, set_rules, restore_backup, pin_backup, unpin_backup, delete_backup, allowlist, set_properties, auto_update, update_server]
      description: What to do to the server.
    slot:
      type: string
      description: A world slot like slot2 (see mc_worlds) — for the world actions, restore_backup, and optionally backup.
    backup:
      type: string
      description: A backup's file name from mc_backups — for restore_backup, pin_backup, unpin_backup, delete_backup.
    label:
      type: string
      description: A short label for a backup.
    name:
      type: string
      description: create_world / update_world — the world's name.
    seed:
      type: string
      description: create_world / reset_world (new_seed) — the seed.
    gamemode:
      type: string
      enum: [survival, creative, adventure]
    difficulty:
      type: string
      enum: [peaceful, easy, normal, hard]
    cheats:
      type: boolean
    mode:
      type: string
      enum: [same_seed, new_seed, empty]
      description: reset_world — regenerate from the same seed, a new seed, or an empty world.
    rules:
      type: object
      description: set_rules / create_world — game rule ids to values, e.g. keepInventory true, doFireTick false.
    add:
      type: string
      description: allowlist — a gamertag to let in.
    remove:
      type: string
      description: allowlist — a gamertag to remove.
    enabled:
      type: boolean
      description: allowlist — turn the allowlist on or off; auto_update — on or off.
    properties:
      type: object
      description: set_properties — server.properties keys to values (null removes an override); they apply at the next start.
  required: [action]
---
Change the server itself: start, stop or restart it, back it up, load, create, change,
reset or restore a world, set game rules, pin or delete backups, change the allowlist or
server.properties, or update the server. Every action is staged as a card Jeff approves —
nothing happens until he does. Loading, resetting and restoring back up what they replace
first. Read mc_worlds / mc_backups first for slot and backup names.
