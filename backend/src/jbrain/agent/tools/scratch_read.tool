---
name: scratch_read
version: 1
permission: web
params:
  type: object
  properties:
    filename:
      type: string
      description: The name of the file to read.
  required: [filename]
---
Read one of your scratchpad files in full by name. These are your own notes from past
nights — who you met, what you meant to come back to, whatever you decided to keep.
