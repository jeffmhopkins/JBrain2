---
name: read_list
version: 1
permission: read
params:
  type: object
  properties:
    list_id:
      type: string
      description: The id of the list to read (from read_lists).
  required: [list_id]
---
Read one of the owner's lists by id, with every item and whether it's checked
off. Each item line includes the item id — use it to check an item off
(check_list_item) or remove it (remove_list_item). Returns a not-in-scope
message if no such list is visible in this session.
