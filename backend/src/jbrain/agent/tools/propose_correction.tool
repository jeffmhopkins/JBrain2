---
name: propose_correction
version: 1
permission: sensitive
params:
  type: object
  properties:
    correction:
      type: string
      description: The correction or new fact to record, in plain prose.
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
