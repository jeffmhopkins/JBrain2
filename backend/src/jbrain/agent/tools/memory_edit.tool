---
name: memory_edit
version: 1
permission: mutate
params:
  type: object
  properties:
    block_id:
      type: string
      description: The id of the task block to edit (from memory_read).
    op:
      type: string
      description: One of add, update, or remove.
    text:
      type: string
      description: The bullet text, for add or update.
    target:
      type: integer
      description: The 0-based bullet index, for update or remove.
  required: [block_id, op]
---
Update your current TASK scratchpad — add, update, or remove a single bullet of
your in-progress plan or notes, one bullet at a time (never a full rewrite, which
would lose what you have accumulated). Only task memory is editable this way; your
persona and the owner's behavioral preferences change only with the owner's
explicit confirmation, never by you.
