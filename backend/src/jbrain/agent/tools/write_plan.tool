---
name: write_plan
version: 3
permission: web
side_effecting: true
params:
  type: object
  properties:
    body:
      type: string
      description: The full plan text (Markdown). This REPLACES the whole plan, so include everything you want to keep — read_plan first, then write the merged result. Use a `- [ ]` / `- [x]` checklist for the steps so progress is visible.
    status:
      type: string
      enum: [not_approved, in_work]
      description: "Set the plan's status. `not_approved`: a draft awaiting the owner's approval (use this when you first draft a plan or substantially revise one — then ask the owner to approve it). `in_work`: you've started executing an APPROVED plan (only allowed once the owner has approved). You CANNOT set `approved` — only the owner can, by tapping Approve; never claim a plan is approved yourself."
    pause:
      type: string
      enum: [checkpoint, await_owner]
      description: "How this turn should hand off. `checkpoint`: you finished a coherent step and marked it done — the system pauses briefly, then continues the next step automatically (this is the normal case; you can also just end your turn). `await_owner`: you are BLOCKED and need the owner's input before the next step — this stops the auto-continue loop until they reply. Use `await_owner` whenever you ask the owner a question mid-plan, so you don't barrel ahead over it."
  required: []
---
Create, replace, or re-status the plan for THIS conversation. Provide `body` to
write/rewrite the plan text, `status` to change its stage, or both.

Use this ONLY when the owner has explicitly asked you to make a plan ("make a plan",
"plan this out", "outline an approach and I'll approve it"). Do NOT draft a plan on
your own for an ordinary multi-step task — planning mode is owner-initiated, not the
default. Once asked, draft it with `write_plan(body=…, status="not_approved")` and ask
the owner to review and approve it — do not start the work yet. The owner approves it (or
edits it first); only then is it `approved`. Once approved, follow it: as you begin
executing, set `status="in_work"`, and keep the body's checklist up to date
(`write_plan(body=…)`) as you complete steps. You may set `not_approved` or
`in_work`, but NEVER `approved` — that's the owner's call. If the owner asks for
changes big enough to need a fresh sign-off, rewrite the body and set
`not_approved` again.

Working an approved plan runs across turns: do one coherent step, mark it `- [x]`
with `write_plan(body=…)`, and end your turn — the system pauses for a short window
(the owner can redirect) and then continues you into the next step automatically. If
you need the owner's input before you can proceed, call `write_plan(pause="await_owner")`
so the loop waits for them instead of auto-continuing over your question.
