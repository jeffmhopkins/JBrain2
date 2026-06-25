---
name: gmail_archive
version: 1
permission: web
side_effecting: true
params:
  type: object
  properties:
    message_id:
      type: string
      description: The message to archive (from gmail_search).
  required: [message_id]
---
Archive a Gmail message — remove it from the inbox while keeping it (and any labels
you applied) in All Mail. This is the non-destructive "done with the inbox" move; it
never deletes. Label the message first if you want it filed somewhere findable.
