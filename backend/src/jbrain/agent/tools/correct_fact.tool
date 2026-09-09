---
name: correct_fact
version: 2
permission: sensitive
mutating: true
side_effecting: true
cost_class: standard
params:
  type: object
  properties:
    entity:
      type: string
      description: >-
        The entity the wrong value is on — its id from find_entity or read_entity, or
        its exact name as read_entity printed it.
    predicate:
      type: string
      description: >-
        The relation whose value is wrong, spelled as read_entity shows it — homeLocation,
        worksAt, bodyWeight, treatedBy.
    qualifier:
      type: string
      description: >-
        The part of the predicate that names WHICH one, when it has parts —
        "kids" for name.nickname.kids. An empty string when the predicate has no parts,
        which is nearly always.
    object:
      type: string
      description: >-
        The value that is actually right, written plainly — "412 Oak St", "178 lb".
        When the right answer is another person, organization or place, pass ITS ID from
        find_entity, and the fact becomes an edge to it. Not a description of what is
        wrong.
    statement:
      type: string
      description: >-
        The corrected fact as one plain sentence, the way it should read back months
        from now — "Jeff lives at 412 Oak St."
    when:
      type: string
      description: >-
        When the corrected value starts holding, as an ISO date Jeff actually gave:
        2026, 2026-03, 2026-03-14, or a full timestamp. An empty string when he gave no
        date. Never guess one.
  required: [entity, predicate, qualifier, object, statement, when]
examples:
  - entity: 0f7a1c4e-2b3d-4a5f-8c9d-0e1f2a3b4c5d
    predicate: homeLocation
    qualifier: ""
    object: 412 Oak St
    statement: Jeff lives at 412 Oak St.
    when: 2026-03
---
Jeff says a value on file is wrong. Record what is actually right, in his authority
rather than the note's: it replaces the current value outright and is PINNED, so nothing
read out of a later note quietly flips it back.

Use it only when he has actually disputed something. It is not the tool for what the
note says — that is assert_fact, and a newer value there supersedes an older one by
itself. Reach for this when he corrects you.

The address is the entity plus the relation, the way read_entity prints them — there is
no fact id to quote and you do not need one. If that address holds several values at once
(someone owns three things, attended four schools) they are co-equal — each is its own
fact, not a version of one value — so nothing is written and you get them listed back.
Tell Jeff what is on file and ask him which one he means; there is no way to replace one
of them with this tool.

`object` is the RIGHT value, not a description of the wrong one. "not Pine Ave" records
a fact that says "not Pine Ave".

Read the result. It tells you what the old value was and that it has been kept as
history — say that back to him, so he can see the correction landed on the thing he
meant. If it says nothing was changed, nothing was: do not tell him it is fixed.
