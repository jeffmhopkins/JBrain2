---
name: gmail_sender_breakdown
version: 1
permission: web
params:
  type: object
  properties:
    query:
      type: string
      description: A Gmail query bounding what to analyze, e.g. "in:anywhere" for the whole mailbox, "in:inbox", "newer_than:1y", "older_than:5y label:newsletters".
    by:
      type: string
      description: Group senders by "domain" (default) or by full "address".
    sample:
      type: integer
      description: How many recent matching messages to sample (default 200, max 500).
  required: [query]
---
See who actually fills the mailbox: sample recent messages matching the query and rank
the senders by volume, grouped by domain (default) or full address. Gmail has no
server-side group-by, so NEVER guess which senders or domains are common — call this to
SEE the real ones. This samples the most recent matches (not a full-history scan), so
treat the ranking as "busiest among recent mail". The workflow: gmail_sender_breakdown
to find the big domains, then gmail_count for an exact per-sender total, then
gmail_bulk_label to file them. Pass in:anywhere to cover the whole mailbox.
