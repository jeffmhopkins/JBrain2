---
name: mc_memory_replace
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
      description: The corrected fact.
  required: [line, text]
---
Correct one remembered line about this chat's player. The old wording is kept in
history, not lost.
