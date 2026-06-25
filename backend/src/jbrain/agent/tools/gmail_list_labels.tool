---
name: gmail_list_labels
version: 1
permission: web
params:
  type: object
  properties: {}
  required: []
---
List the labels that already exist in the owner's Gmail — the current label taxonomy.
ALWAYS check this before creating a new label, and prefer applying an existing label
over inventing a near-duplicate, so the taxonomy stays clean. Gmail nests labels with
a "Parent/Child" naming convention (e.g. "Finance/Taxes/2008").
