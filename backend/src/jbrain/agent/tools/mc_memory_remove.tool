---
name: mc_memory_remove
version: 1
permission: web
side_effecting: true
params:
  type: object
  properties:
    line:
      type: integer
      description: The line number from mc_memory_read.
    text:
      type: string
      description: The line's exact current text, so the wrong line can never be removed.
  required: [line, text]
---
Forget one remembered line about this chat's player (it is kept in history).
