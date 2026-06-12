---
name: read_lists
version: 1
permission: read
params:
  type: object
  properties:
    include_archived:
      type: boolean
      description: Include archived lists too. Defaults to false (open lists only).
  required: []
---
List the owner's lists in this session's scope — shopping lists, packing lists,
watchlists, and the like. Returns each list's title, domain, open/total item
counts, and id. Use the id to read a specific list (read_list) or add to it
(add_list_item). Returns nothing if there are no lists in scope.
