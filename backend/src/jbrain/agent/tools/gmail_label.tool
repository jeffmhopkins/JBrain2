---
name: gmail_label
version: 1
permission: web
side_effecting: true
params:
  type: object
  properties:
    message_id:
      type: string
      description: The message to relabel (from gmail_search).
    add:
      type: array
      items:
        type: string
      description: Label names to apply. Each must already exist — create it with gmail_create_label first.
    remove:
      type: array
      items:
        type: string
      description: Label names to remove from the message.
  required: [message_id]
---
Apply or remove labels on a Gmail message — this is how you "move" mail into a
folder/label. Pass label NAMES (not ids) in `add` and/or `remove`. A name in `add`
must already exist; if it doesn't, this tool tells you so rather than inventing it —
create it with gmail_create_label first (this avoids typo'd duplicate labels). To
move a message out of the inbox entirely, label it and then gmail_archive it.
