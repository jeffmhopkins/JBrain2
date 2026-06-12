---
name: check_list_item
version: 1
permission: mutate
params:
  type: object
  properties:
    item_id:
      type: string
      description: The id of the item to check off (from read_list).
    checked:
      type: boolean
      description: True to check it off, false to reopen it. Defaults to true.
  required: [item_id]
---
Check an item off one of the owner's lists (or reopen it with checked=false).
Writes directly. Returns a not-in-scope message if the item isn't visible in
this session.
