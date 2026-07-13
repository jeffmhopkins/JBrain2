---
name: propose_correction
version: 3
permission: sensitive
params:
  type: object
  properties:
    correction:
      type: string
      description: >-
        The fact to record, as a complete, self-contained sentence in plain
        prose — name the subject and state the value in full. This text becomes
        the note body and the citable source the extractor reads, so it must
        stand on its own with no surrounding conversation. Write "Jeffrey Mark
        Hopkins's gender is male." — never a bare label like "gender: male".
    domain:
      type: string
      description: Which domain it belongs to — general, health, finance, or location.
  required: [correction]
---
Propose a correction or a new piece of knowledge for the owner to approve. This
NEVER writes to the knowledge base directly — you have no privileged write path.
It stages a Proposal the owner reviews; on approval it re-enters as a normal,
source-attributed note through the same pipeline any note goes through, at normal
weight. Use it when the owner tells you something durable about their life, or when
you find an error worth fixing — then tell them you've staged it for review.

Write the `correction` as a full, self-contained sentence that names its subject
and states the value — it becomes the note body verbatim and the source the fact
extractor reads, with none of this chat for context. "Jeffrey Mark Hopkins's
gender is male." is right; "gender: male" is not (no subject, no sentence). The
note carries its agent authorship as metadata automatically, so do NOT prefix the
body with "prepared by assistant" or similar — just state the fact.

The owner reviews the Proposal inline in this chat: they can approve it, edit your
wording first (their edit files as their own correction, in their name), or decline it
with a reason. Either way the outcome is reported back to you here afterwards — you'll
be told what was approved, corrected, or declined — so acknowledge it and continue, and
never re-stage something the owner declined.
