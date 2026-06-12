---
name: add_list_item
version: 1
permission: mutate
params:
  type: object
  properties:
    list_id:
      type: string
      description: The id of the list to add to (from read_lists or create_list).
    body:
      type: string
      description: The item text, e.g. "eggs" or "call the dentist".
  required: [list_id, body]
---
Add an item to one of the owner's lists. Writes directly (lists are the owner's
own data) and appends to the end. Returns the new item's id so you can later
check it off or remove it. Returns a not-in-scope message if the list isn't
visible in this session.
