---
name: click
version: 1
permission: web
params:
  type: object
  properties:
    ref:
      type: string
      description: The element's ref from the LATEST page, e.g. "e46".
  required: [ref]
---
Click a link, button, tab, menu entry or option on the current page. Buttons that buy,
book, sign up, log in, send or submit are refused.
