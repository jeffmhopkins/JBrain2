---
name: lookup_medication
version: 1
permission: external
params:
  type: object
  properties:
    name:
      type: string
      description: The medication name to look up (e.g. metformin).
  required: [name]
---
Look up reference information about a medication — its ingredients, dose forms, and
related concepts — from the NLM drug database. This is an OFF-BOX lookup, so it does
not run on its own: it stages the exact outbound request (a medication name, nothing
else) for the owner to approve, and the call is made only after approval. The result
is reference data to cite with its source, never a fact about the owner.
