---
name: gmail_create_label
version: 1
permission: web
side_effecting: true
params:
  type: object
  properties:
    name:
      type: string
      description: The label to create, using "Parent/Child" for nesting (e.g. "Finance/Taxes/2008").
  required: [name]
---
Create a new Gmail label, using "Parent/Child" names to build a nested taxonomy
(e.g. "Finance/Taxes/2008"). Check gmail_list_labels first and reuse an existing
label wherever one fits — only create a label when nothing suitable exists. Creating
a label that already exists simply returns the existing one (no duplicate).
