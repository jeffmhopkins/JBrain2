---
name: grok_outline
version: 1
permission: web
params:
  type: object
  properties:
    slug:
      type: string
      description: The article slug from grok_search (e.g. Elon_Musk).
  required: [slug]
---
Return a Grokipedia article's table of contents — its section headings as a numbered
tree, plus when it was last updated and its categories — WITHOUT the body text. This
is the cheap way to see how a long article is organized before reading any of it: get
the outline, then read only the section you need with grok_section, passing either the
section number (e.g. "2.1") or its title. Always prefer outline → section over pulling
a whole article into the conversation.
