---
name: gmail_search
version: 1
permission: web
params:
  type: object
  properties:
    query:
      type: string
      description: A Gmail search query using Gmail's own operators, e.g. "from:bank@example.com before:2012/01/01", "older_than:5y subject:invoice", "has:attachment label:receipts".
    limit:
      type: integer
      description: Maximum messages to return (default 25, max 100).
  required: [query]
---
Search the owner's Gmail and return the matching messages — each with its id, sender,
subject, date, and a short snippet. Build the query from Gmail's own search operators
(from:, to:, subject:, before:/after:, older_than:, has:attachment, in:inbox, label:)
to narrow to exactly the slice you are triaging. This returns metadata only — open a
message with gmail_read before deciding where it belongs. The ids it returns are what
gmail_read, gmail_label, and gmail_archive act on.
