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
    old_text:
      type: string
      description: The line's exact current text, so a shifted number can never change the wrong line.
    text:
      type: string
      description: The corrected fact.
  required: [line, old_text, text]
---
Correct one remembered line about this chat's player. The old wording is kept in
history, not lost.
