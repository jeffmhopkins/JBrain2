---
name: tabs
version: 1
permission: web
params:
  type: object
  properties:
    action:
      type: string
      description: list, select, close or new.
    index:
      type: integer
      description: The tab number, for select or close.
    url:
      type: string
      description: The address to open, for new.
  required: [action]
---
List the open tabs, switch to one, close one, or open a new one. A link that opened in a
new tab shows up here.
