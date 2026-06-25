---
name: gmail_bulk_label
version: 1
permission: web
side_effecting: true
params:
  type: object
  properties:
    query:
      type: string
      description: A Gmail search query selecting the messages to relabel, e.g. "from:chase.com", "from:(*@chase.com) older_than:5y".
    add:
      type: array
      items:
        type: string
      description: Label names to apply to every match. Each must already exist (create with gmail_create_label first).
    remove:
      type: array
      items:
        type: string
      description: Label names to remove from every match. Use "INBOX" to bulk-archive (move out of the inbox).
  required: [query]
---
Apply or remove labels across EVERY message matching a query, in one operation — the
bulk way to "move" a whole sender or domain into a label. Pass label NAMES; an `add`
label must already exist (create it first; this tool won't invent labels). Put "INBOX"
in `remove` to bulk-archive the matches as you label them. This is high-leverage and
high-blast-radius: ALWAYS run gmail_count on the same query first and state how many
messages it will touch before doing it. It never deletes. Matches beyond the safety
cap are reported so you can narrow the query and finish the rest.
