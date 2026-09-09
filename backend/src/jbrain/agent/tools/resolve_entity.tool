---
name: resolve_entity
version: 1
permission: mutate
mutating: true
side_effecting: true
cost_class: standard
params:
  type: object
  properties:
    entities:
      type: array
      maxItems: 12
      description: >-
        Every person, place, organization, animal, condition, medication or thing the
        note names. Send them all in ONE call.
      items:
        type: object
        properties:
          surface:
            type: string
            description: >-
              The name exactly as the note writes it — "Dana Whitfield", "Ritual",
              "Everlane". Never a role word like "her partner" or "my doctor".
          kind:
            type: string
            description: >-
              What sort of thing it is, one word: person, organization, place, event,
              condition, medication, animal, or thing.
        required: [surface, kind]
  required: [entities]
examples:
  - entities:
      - surface: Dana Whitfield
        kind: person
      - surface: Everlane
        kind: organization
      - surface: Ritual
        kind: place
---
Turn the names this note uses into handles you can then record facts about. Send the
whole cast of the note in one call — up to 12 — not one call per name.

Each name comes back with a handle (`e1`, `e2`, …), the kind, the domain it is filed
under, and whether it was already known or newly created. Use those handles as the
`subject` and `object` of assert_fact. This is the only way to introduce an entity:
assert_fact will not accept a name it has never seen resolved.

Pass the name the note actually writes, not a role. "Her partner Theo" resolves as
"Theo"; "my doctor" is not a name at all — if the note never names them, there is
nothing to resolve, and the fact you wanted to record about them is one to ask about
instead.

A name that matches several of the owner's existing entities comes back unresolved and
with no handle, deliberately: guessing which one is meant is how a fact ends up on the
wrong person for good. Say what distinguishes them from the note, or leave it out and
ask the owner.

Resolving the same name twice returns the same handle; it costs nothing but does not
help either.
