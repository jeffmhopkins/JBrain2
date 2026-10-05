---
name: navigate
version: 1
permission: web
params:
  type: object
  properties:
    url:
      type: string
      description: The full http(s) address to open.
  required: [url]
---
Open a web address in the current tab. Use it to reach the site the goal names, or a page
whose address you saw. Only public http and https sites can be opened.
