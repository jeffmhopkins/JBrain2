---
name: name_session
version: 1
permission: mutate
params:
  type: object
  properties:
    name:
      type: string
      description: A short, specific title for this chat — 3 to 6 words, Title Case, no quotes and no trailing punctuation. Name the topic the way a person would label the conversation to find it later.
  required: [name]
---
Name this chat so the owner can find it later. Call this ONCE, in your first reply
to a conversation that has no name yet — the turn context tells you when that is.
Do not call it again afterwards, and never to rename a chat the owner already
named: a call against an already-named chat is refused and wastes a step.

Pick a concrete topic label, not a description of the exchange — "Roof Quote
Comparison", not "User Asks About Roofing" and never "Chat" or "Conversation".
Answer the owner's actual message in the same reply; naming the chat is
bookkeeping alongside your answer, not instead of it.
