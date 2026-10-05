---
name: select_option
version: 1
permission: web
params:
  type: object
  properties:
    ref:
      type: string
      description: The dropdown's ref from the LATEST page.
    values:
      type: array
      items:
        type: string
      description: The option label(s) to choose, as shown on the page.
  required: [ref, values]
---
Choose an option in a dropdown that picks a location, a store or theater, a date, a sort
order or a filter. Other dropdowns are refused.
