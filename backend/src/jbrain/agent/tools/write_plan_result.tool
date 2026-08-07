---
name: write_plan_result
version: 1
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
Append your finished step's SYNTHESIS to this plan's shared results scratchpad. The
scratchpad is APPEND-ONLY and index-ordered: each call adds a new entry at the next index
and NEVER overwrites an earlier one, so nothing you or a later step records is ever erased.

Use it while executing an approved plan: after you complete a step (before you mark it
`- [x]` with write_plan), record the step's actual findings here with `note` (and a short
`heading`). Later steps read the whole scratchpad back via `read_plan`, and the final step
reads all of it to write the deliverable — so put the real substance here, not just "done".
Read the plan first if you want to see what earlier steps already recorded.
