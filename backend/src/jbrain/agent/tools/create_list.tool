---
name: create_list
version: 1
permission: mutate
params:
  type: object
  properties:
    title:
      type: string
      description: The list's title, e.g. "Groceries" or "Packing for Tahoe".
    domain:
      type: string
      description: Which domain it belongs to — general, health, finance, or location. Defaults to this session's scope.
  required: [title]
---
Create a new, empty list for the owner. Lists are the owner's own data that you
maintain directly — this writes immediately, no approval needed (unlike
propose_correction, which stages knowledge for review). Returns the new list's
id so you can add items to it (add_list_item). You can only create a list in a
domain this session is scoped to.
