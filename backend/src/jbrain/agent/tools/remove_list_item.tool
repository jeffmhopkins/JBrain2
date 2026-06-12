---
name: remove_list_item
version: 1
permission: mutate
params:
  type: object
  properties:
    item_id:
      type: string
      description: The id of the item to remove (from read_list).
  required: [item_id]
---
Remove an item from one of the owner's lists. Writes directly and deletes the
item outright (lists are the owner's own data, not citable history). Returns a
not-in-scope message if the item isn't visible in this session.
