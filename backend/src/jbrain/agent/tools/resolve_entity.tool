---
name: resolve_entity
version: 2
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
              condition, medication, animal, product, device, vehicle, or thing.
          distinguish:
            type: string
            description: >-
              An empty string almost always. Fill it only when a name came back
              AMBIGUOUS and the note itself says which one is meant — "the cardiologist",
              "Dana Whitfield", "the one in Boulder". Words from the NOTE, not a guess
              and not an id: they are matched against the names, kinds and summaries the
              ambiguous result listed for you. If the note does not say which, leave it
              empty and ask the owner.
        required: [surface, kind, distinguish]
  required: [entities]
examples:
  - entities:
      - surface: Dana Whitfield
        kind: person
        distinguish: ""
      - surface: Everlane
        kind: organization
        distinguish: ""
      - surface: Ritual
        kind: place
        distinguish: ""
      - surface: Dana
        kind: person
        distinguish: the one in Boulder
---
Turn the names this note uses into handles you can then record facts about. Send the
whole cast of the note in one call — up to 12 — not one call per name.

Each name comes back with a handle (`e1`, `e2`, …), the kind, the domain it is filed
under, and whether it was already known or newly created. Use those handles as the
`subject` and `object` of the fact you record. This is the only way to introduce an
entity: no other tool will accept a name it has never seen resolved.

A name the owner already had comes back with WHAT IS ON FILE about it — the facts the
graph holds now, newest first. Read them before you record. If the note gives a newer
value for something listed there, record it: a new value supersedes the old one and the
old one is kept as history. If the note DISAGREES with one and you cannot tell which is
right from the note alone, that is the case for asking the owner — do not record both
and leave him two contradictory facts.

Pass the name the note actually writes, not a role. "Her partner Theo" resolves as
"Theo"; "my doctor" is not a name at all — if the note never names them, there is
nothing to resolve, and the fact you wanted to record about them is one to ask about
instead.

A name that matches several of the owner's existing entities comes back unresolved and
with no handle, deliberately: guessing which one is meant is how a fact ends up on the
wrong person for good. The result NAMES them — each candidate with its kind and what is
known about it — so re-send that one name with `distinguish` set to what the note says
about which one is meant. If the note does not say, leave it out and ask the owner.

Resolving the same name twice returns the same handle; it costs nothing but does not
help either.
