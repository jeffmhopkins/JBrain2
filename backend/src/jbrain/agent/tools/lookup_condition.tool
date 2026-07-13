---
name: lookup_condition
version: 2
permission: external
params:
  type: object
  properties:
    name:
      type: string
      description: The condition name to look up (e.g. hypertension).
  required: [name]
---
Look up a plain-language overview of a medical condition from the NLM consumer-health
service. This is an OFF-BOX lookup: it does not run on its own but stages the exact
outbound request (a condition name, nothing else) for the owner to approve, and the
call is made only after approval. The result is reference data to cite with its
source, never a fact about the owner.
After the owner approves or declines the lookup, the outcome is reported back to you
in this chat, so continue from what actually ran.
