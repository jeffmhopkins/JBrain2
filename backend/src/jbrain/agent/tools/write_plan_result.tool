---
name: write_plan_result
version: 2
permission: web
side_effecting: true
params:
  type: object
  properties:
    note:
      type: string
      description: The synthesized RESULT of the step you just did (Markdown) — the actual findings/output, distilled, that later steps and the final write-up will build on. Not a status line ("step 1 done"); the substance. Keep it self-contained.
    heading:
      type: string
      description: A short label for this entry, e.g. "Step 1 — Top-rated carry-ons". Optional but recommended so the scratchpad reads as an ordered log.
  required: [note]
---
Record a finished step. This APPENDS your step's synthesis to the plan's shared results
scratchpad AND completes the step for you: it ticks the next unchecked box (`- [x]`) and, the
first time, flips the plan to `in_work`. The scratchpad is append-only and index-ordered, so
nothing you or a later step records is ever erased.

Call it ONCE per step, right after you finish that step's work — you do NOT need a separate
write_plan call to check the box off. Put the real substance in `note` (findings, a
comparison, a draft), not "done". Later steps read the whole scratchpad back via `read_plan`,
and the final step reads all of it to write the deliverable. After you call this, END YOUR
TURN so the owner gets a moment before the next step auto-continues.
