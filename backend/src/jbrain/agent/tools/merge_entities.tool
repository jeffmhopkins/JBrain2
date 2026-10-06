---
name: merge_entities
version: 3
permission: sensitive
mutating: true
side_effecting: true
cost_class: standard
params:
  type: object
  properties:
    entity_a:
      type: string
      description: >-
        One of the two — its id from find_entity, or its exact name as find_entity
        printed it.
    entity_b:
      type: string
      description: The other one, the same way.
    reason:
      type: string
      description: >-
        One sentence saying what makes them the same thing — "Jeff says the Dana in this
        note is the Dana Whitfield already on file." An empty string if he simply said so.
    keep_name:
      type: string
      description: >-
        Which of the two names the one record left should carry — exactly one of the
        two names as find_entity printed them. Set it whenever Jeff has said which
        spelling is right. An empty string when he has not.
  required: [entity_a, entity_b, reason, keep_name]
examples:
  - entity_a: 0f7a1c4e-2b3d-4a5f-8c9d-0e1f2a3b4c5d
    entity_b: 7c1b9a30-55de-4f2a-9a11-2b6c0d8e4f31
    reason: Jeff says the Dana in this note is the Dana Whitfield already on file.
    keep_name: ""
  - entity_a: Dr. Brochia
    entity_b: Dr. Barochia
    reason: Jeff says Barochia is the proper spelling.
    keep_name: Dr. Barochia
---
Two entities turn out to be one thing. Stage the fold for Jeff to approve.

This does NOT merge them. Folding two entities together moves every mention and every
fact from one onto the other across all of his domains at once, which is a write this
conversation is not permitted to make — it can only put the question in front of him.
So nothing changes when you call this. Tell him it is waiting on him; never say they
have been merged.

You do not choose which one survives, and there is no argument to say so. When he
approves, the more-anchored identity is kept and the other's mentions and facts repoint
onto it, so nothing is lost either way.

The NAME is a different matter, and it is yours to carry. The survivor is usually the
older record, so when he corrects a spelling ("Barochia is the proper spelling") the
survivor is the misspelled one — pass `keep_name` with the spelling he gave, and the one
record left takes that name (the old spelling stays behind as an alias, so the note's
text still finds it). With `keep_name` empty the survivor keeps whatever name it had.

A correction to a spelling is first a question of whether the right spelling is ALREADY
on file — often as a fuller name ("Dr. Amit Barochia"). Look with find_entity before you
mint a new record for it; when it is there, the fold is the misspelled record into that
one, and no new record is needed.

Use it when he tells you two records are the same person, place or thing, or when he
confirms it after you asked.

find_entity only sees this note's domain. A record he names that it cannot find may be
filed in another of his domains (health, finance, location) — pass the name exactly as
he gave it, and this looks it up across his records. If it is found the card is staged
like any other; if it is not, say plainly that no record by that name exists, never
"try again later". Not on a hunch about two similar names — a wrong fold is
his to undo, and the point of asking him is that he knows and you do not. If he has
already said they are different, this refuses and says so.
