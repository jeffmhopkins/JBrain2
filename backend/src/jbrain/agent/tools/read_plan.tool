---
name: read_plan
version: 1
permission: web
params:
  type: object
  properties: {}
  required: []
---
Read the current plan for THIS conversation — its full text and its status
(`not_approved`, `approved`, or `in_work`). A plan is a shared, owner-visible
document for a multi-step task: what you're going to do and where you are in it.
Call this when the owner refers to "the plan", asks you to continue or check
progress, or before you revise it, so you build on what's there instead of starting
over. Returns "(no plan yet)" when this conversation has none.
