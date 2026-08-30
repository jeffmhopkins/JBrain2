---
name: name_session
version: 2
permission: mutate
params:
  type: object
  properties:
    name:
      type: string
      description: A short, specific title for this chat — 3 to 6 words, Title Case, no quotes and no trailing punctuation. Name the topic the way a person would label the conversation to find it later.
  required: [name]
---
Name this chat so the owner can find it later. Call this ONCE, in your first reply to a
conversation that has no name yet — the turn context tells you when that is. Do not call
it again afterwards, and never to rename a chat the owner already named: a call against
an already-named chat is refused and wastes a step.

A name only takes effect as a TOOL CALL. Writing the name into your reply instead — "I've
named this chat X", a `{"name": ...}` block, a parenthetical — names nothing; the chat
stays untitled while you tell the owner otherwise. Make the call or say nothing about
naming at all.

Pick a concrete topic label, not a description of the exchange — "Roof Quote Comparison",
not "User Asks About Roofing". Almost every opening message is nameable, a short question
included. The ONE exception is a message with no subject in it at all — a bare "Hi",
"hey", "good morning" — where there is simply nothing to label: answer it, name nothing,
and name the chat on the next message instead, which the turn context will prompt you to
do. Do NOT invent a name for that turn: "General Greeting", "General Inquiry", "Chat" and
any other synonym for "conversation" are refused by this tool, and naming is one-way, so
a placeholder that did land could never be corrected.

Answer the owner's actual message in the same reply; naming the chat is bookkeeping
alongside your answer, not instead of it.
