---
name: press_key
version: 1
permission: web
params:
  type: object
  properties:
    key:
      type: string
      description: One of Enter, Tab, Escape, ArrowDown, ArrowUp, ArrowLeft, ArrowRight, PageDown, PageUp, Home, End.
  required: [key]
---
Press one key, e.g. Escape to close a pop-up or ArrowDown to move through a list.
