---
name: scratch_read
version: 2
permission: web
params:
  type: object
  properties:
    filename:
      type: string
      description: The name of the file to read.
    history:
      type: boolean
      description: >-
        List the file's earlier versions instead of its current contents — a date, what kind
        of change it was, and a size for each. Cheap; it does not include their text.
    version:
      type: integer
      description: >-
        Read one earlier version by its number from the history list (1 is the most recent).
  required: [filename]
examples:
  - {filename: "index.md"}
  - {filename: "people.md", history: true}
  - {filename: "people.md", version: 2}
---
Read one of your scratchpad files in full by name. These are your own notes from past
nights — who you met, what you meant to come back to, whatever you decided to keep. Every
change you make is kept: if a file looks wrong, or you replaced something you wanted, ask
for history=true to see its earlier versions and then read one by number.
