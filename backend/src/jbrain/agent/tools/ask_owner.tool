---
name: ask_owner
version: 2
permission: mutate
side_effecting: true
params:
  type: object
  properties:
    questions:
      type: array
      maxItems: 5
      description: >-
        Everything about this note you cannot settle, together in ONE call. Jeff sees
        them as one set and answers them in one go, so an answer to one can inform
        another.
      items:
        type: object
        properties:
          question:
            type: string
            description: >-
              The question, in one sentence, naming the specific thing you cannot settle
              from the note — "Which Sarah is this, your sister or Sarah Chen from
              work?", not "Can you tell me more about this note?".
          blocks:
            type: string
            description: >-
              What this is blocking, in a few words — the predicate you cannot write or
              the resolve call you are stuck on ("who ran the 10k with you", "the dose
              on the second line").
          candidates:
            type: string
            description: >-
              The candidates as a short comma-separated list, where the resolver handed
              you any — "Sarah Whitfield (sister, 12 notes), Sarah Chen (work, 3
              notes)". This is what lets Jeff answer with one tap instead of typing.
        required: [question]
  required: [questions]
examples:
  - questions:
      - question: Which Sarah is this — your sister, or Sarah Chen from the running club?
        blocks: who the note's "with Sarah" resolves to
        candidates: Sarah Whitfield (sister, 12 notes), Sarah Chen (running club, 3 notes)
      - question: Is the dose on the second line 25 mg or 2.5 mg?
        blocks: the medication dose
        candidates: ""
---
Record what you cannot settle about this note for Jeff, and stop. Ask EVERYTHING you
are stuck on in this one call — Jeff answers the whole set in one go, so three questions
here cost him one trip and one re-read of the note, where three separate asks cost three
of each.

The bar for a question is unchanged: you genuinely cannot settle it from the note. Two
people in it share a name and nothing tells them apart, or the meaning turns on a date
the note never gives, or a word is smudged past reading. Not to have a reading approved,
not for anything the note answers on a second read, and not as a hedge — you commit your
reading and Jeff corrects you by replying, so a question you did not need costs more than
a wrong link would. Anything you CAN settle, settle and write first.

`blocks` and `candidates` are what let Jeff answer with one tap. `blocks` says which
predicate or which resolve call is stuck, so he can see what his answer buys; `candidates`
lists what the resolver already handed you, so he picks rather than types. Leave
`candidates` empty when there were none — never invent one.

Calling this ENDS YOUR TURN. Nothing you were going to do after it happens, so do the
writing first and ask last, in that order, in the same turn. What you already wrote
stands.

The note then waits on Jeff — there is no nagging and no deadline, and nothing else runs
over this note until he answers. When he does, each answer is appended to the note as a
dated clarification (it becomes part of the note's text, so it is a source like the rest
of the note), the note is re-read from scratch, and you take up the thread with the full
tool set. He may answer some and not others; you are told which ones he left open, and
they are yours to re-ask, work around, or drop.

Ask once. A second ask_owner in the same turn is refused: the first one already ended it.
