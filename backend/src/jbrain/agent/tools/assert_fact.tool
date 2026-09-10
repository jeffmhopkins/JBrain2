---
name: assert_fact
version: 3
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
              something the graph already has a word for. A few relations name a SLOT
              rather than one value and take a third dotted segment saying which: a
              nickname belongs to the people who use it (name.nickname.friends,
              name.nickname.kids, name.nickname.work), an identifier belongs to its
              scheme (identifier.icd10). Every other relation is two segments at most.
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
          when_end:
            type: string
            description: >-
              Almost always an empty string. Fill it ONLY when the note itself says the
              fact is OVER and says when it ended — "we lived there from 2019 until
              2023" ends 2023 — and then only as an ISO date in the same shapes as
              `when`. If the fact is still true, or the note gives no ending date, the
              answer is an empty string. Never today's date, and never a phrase like
              "present" or "last week".
          quote:
            type: string
            description: >-
              The words in the note this fact rests on, copied out exactly — character
              for character, no paraphrase.
          confidence:
            type: number
            description: >-
              A number from 0 to 1: how sure you are you READ these words correctly.
              Write 1 for almost every fact — the note's words are plain. Write 0.3 or
              lower when you had to GUESS at the words themselves: a blurry photo, bad
              handwriting, an OCR line you could not make out, a digit you could not
              quite see. This is about legibility, never about whether the fact is true,
              whether Jeff is right, or how important it is.
        required:
          [subject, predicate, object, statement, when, when_end, quote, confidence]
  required: [facts]
examples:
  - facts:
      - subject: e1
        predicate: worksAt
        object: e2
        statement: Dana Whitfield works at Everlane as a staff engineer.
        when: 2026-03
        when_end: ""
        quote: started at Everlane as a staff engineer in March
        confidence: 1
      - subject: e1
        predicate: allergy
        object: shellfish
        statement: Dana Whitfield is allergic to shellfish.
        when: ""
        when_end: ""
        quote: is allergic to shellfish
        confidence: 1
      - subject: e1
        predicate: livesIn
        object: e3
        statement: Dana Whitfield lived in Oakland from 2019 until 2023.
        when: 2019
        when_end: 2023
        quote: lived in Oakland from 2019 until 2023
        confidence: 1
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

`confidence` is about your EYES, not your judgement. Almost every note is typed text
and reads as 1. A photo of a pill bottle where the dose is half out of focus is what the
low end is for: a number you had to guess at is recorded, but it cannot silently replace
a value already on file — it is held for Jeff instead. Marking a fact you read perfectly
well as uncertain parks a true fact behind a question he has to answer.

`when_end` closes an interval the note itself closes — "we lived there 2019 to 2023",
"she was at Pied Piper through 2020". Leave it empty for anything still true, which is
nearly everything: an end you did not read in the note retires a fact that has not
ended. An end with no `when`, an end that is not a date, or an end that does not follow
its start is ignored and the fact is recorded open.

You cannot delete, retract or correct anything with this tool, and you do not need to:
a value you record now supersedes the older one by itself, and anything the note stops
saying is dropped when the note is settled.
