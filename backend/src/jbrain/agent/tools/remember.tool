---
name: remember
version: 1
permission: sensitive
params:
  type: object
  properties:
    body_md:
      type: string
      description: The behavioral preference or self-knowledge to remember, as bullets.
    domain:
      type: string
      description: Which domain it concerns — general, health, finance, or location.
  required: [body_md]
---
Propose to remember a durable behavioral preference or piece of self-knowledge —
how the owner wants you to work. This NEVER writes on its own: behavioral memory
changes only with the owner's explicit confirmation, so calling this stages the
change for the owner to approve rather than saving it. Do not use it for world-facts
about the owner's life — those become notes — only for how you should behave.
