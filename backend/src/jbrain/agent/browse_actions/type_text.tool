---
name: type_text
version: 1
permission: web
params:
  type: object
  properties:
    ref:
      type: string
      description: The field's ref from the LATEST page.
    text:
      type: string
      description: What to type — a search term, a city or zip code, a date.
    submit:
      type: boolean
      description: Press Enter after typing (to run a search). Default false.
  required: [ref, text]
---
Type into a search box, a filter, a location or zip-code field, or a date field. Any other
field (a name, email, phone, password, payment, account or message field) is refused, and
so is text that looks like personal data.
