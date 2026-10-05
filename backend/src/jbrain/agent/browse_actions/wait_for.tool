---
name: wait_for
version: 1
permission: web
params:
  type: object
  properties:
    text:
      type: string
      description: Wait until this text appears on the page.
    seconds:
      type: number
      description: Or just wait this many seconds (at most 5).
---
Wait for a slow page: until some text appears, or for a few seconds.
