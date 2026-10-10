---
name: mc_memory_add
version: 1
permission: web
side_effecting: true
params:
  type: object
  properties:
    text:
      type: string
      description: One short fact worth remembering about this player.
  required: [text]
---
Remember one new fact about this chat's player, as a new line. Memory is for durable
facts (their base is at X Z, they're scared of the Deep Dark), not progress — that goes
in mc_log.
