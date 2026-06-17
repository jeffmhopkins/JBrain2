---
name: propose_prompt_edit
version: 1
permission: sensitive
params:
  type: object
  properties:
    target_name:
      type: string
      description: The name of the self-editable prompt or tool to revise (e.g. session.title).
    failure_mode:
      type: string
      description: What the artifact gets wrong and the improvement to make, stated concretely.
  required: [target_name, failure_mode]
---
Draft a versioned change to one of your OWN self-editable prompt/tool definitions and
stage it for the owner's approval. Use this only when the owner asks you to improve how
you behave (e.g. "your chat titles are too long — fix the title prompt"). It reads the
current definition, drafts a revised version with a bumped version, a rationale, and a
new test case, then stages a prompt-edit proposal showing the exact diff. It NEVER applies
the change — the owner reviews the diff and lands it as a code change. The data/instruction
boundary and the domain-classification definitions cannot be edited. Put the artifact's
name in `target_name` and describe the problem to fix in `failure_mode`. If the named
target is not self-editable, say so plainly rather than guessing another.
