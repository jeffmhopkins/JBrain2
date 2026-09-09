---
name: assert_fact
version: 2
permission: mutate
mutating: true
side_effecting: true
cost_class: standard
params:
  type: object
  properties:
    facts:
      type: array
      maxItems: 8
      description: >-
        Everything this note says, as separate facts. Send them together in ONE call.
      items:
        type: object
        properties:
          subject:
            type: string
            description: >-
              The entity this fact is about, as the handle resolve_entity gave you
              ("e1"), or the exact name you resolved.
          predicate:
            type: string
            description: >-
              The relation, in a short lowerCamelCase or snake_case name — worksAt,
              livesIn, spouse, allergy, bodyWeight, medication, treatedBy, birthDate.
              Use the plainest name for the relation; do not invent a new one for
              something the graph already has a word for.
          object:
            type: string
            description: >-
              The other side of the relation. A handle ("e2") when it is another entity
              you resolved; otherwise the value itself, written plainly — "412 Oak St",
              "178 lb", "staff engineer".
          statement:
            type: string
            description: >-
              The fact as one plain sentence, the way it should read back to the owner
              months from now — "Dana Whitfield works at Everlane as a staff engineer."
          when:
            type: string
            description: >-
              When the fact holds, as an ISO date the note actually gives: 2026,
              2026-03, 2026-03-14, or a full timestamp. An empty string when the note
              gives no date. Never guess one.
          quote:
            type: string
            description: >-
              The words in the note this fact rests on, copied out exactly — character
              for character, no paraphrase.
        required: [subject, predicate, object, statement, when, quote]
  required: [facts]
examples:
  - facts:
      - subject: e1
        predicate: worksAt
        object: e2
        statement: Dana Whitfield works at Everlane as a staff engineer.
        when: 2026-03
        quote: started at Everlane as a staff engineer in March
      - subject: e1
        predicate: allergy
        object: shellfish
        statement: Dana Whitfield is allergic to shellfish.
        when: ""
        quote: is allergic to shellfish
---
Record what the note says. One call, up to 8 facts — split the note into the separate
things it claims and send them together, not one call per fact.

Resolve the entities first: `subject` and `object` are handles from resolve_entity. An
`object` that is not a handle is stored as a plain value, which is right for an address,
a dose, a job title, a reading. If the other side is a PERSON or an ORGANIZATION, it
should be a handle — a value there is a name the graph cannot connect to anything.

Record what the note says, and commit to it. A clear fact does not need the owner's
approval; you are showing him what you read, not asking permission. Facts you are
genuinely unsure of are worth recording with the words the note used, not dropping.

`quote` is checked against the note. Copy the passage the fact rests on — for something
the note implies rather than states, the passage the inference rests on. A quote that
does not appear in the note does not stop the fact being recorded; it records it at a
low weight, and a low-weight value that disagrees with a confident one already on file
is HELD for the owner instead of replacing it. That is nearly always worse than just
quoting accurately.

The result tells you what the server did that you did not ask for: a value that replaced
an older one (the old one is kept as history), a fact already on file (nothing changed),
or a fact HELD because it clashes with something already recorded at the same time. A
held fact is not live. Do not re-send it in a different shape — ask the owner which is
right.

You cannot delete, retract or correct anything with this tool, and you do not need to:
a value you record now supersedes the older one by itself, and anything the note stops
saying is dropped when the note is settled.
