# Second observed example — "Jeff ate Celine's dinner" (captured for synthesis)

Owner-supplied screenshot, Jun 11 2026, xai:grok-4.3. A sharper variant of the
"Jeff married Celine Hopkins" lapse — fold into the synthesis after agents A/B/C.

- **Note body:** "Jeff are Celine's dinner last night" (a typo for "ate").
- **Analysis title:** "Jeff ate Celine's dinner"
- **Tags:** dinner, eating, jeff, **celine**, food
- **Mentions:** **only `Jeff`** (Person, provisional) — Celine NOT emitted.
- **Facts:** `ate → "Jeff ate Celine's dinner last night."` (kind=event, 70%).
- **Temporal token:** "last night" → Jun 11 2026 (resolved). Fine.

## Two new signals this adds beyond the marriage case

1. **Possessive-modifier person, not even the verb's object.** Here Celine is
   the *possessor* of the object ("Celine's dinner"); the verb's grammatical
   object is "dinner". The lapse therefore is broader than relationship
   objects: **any** person occupying a non-subject role (possessor, object,
   oblique) tends to be dropped as an entity. There is no relationship/mutual
   status to recover here — it is simply a person who should be a Person
   mention/entity and is not.

2. **Smoking gun — tag without entity.** The model surfaced `celine` in the
   TAGS yet did not promote her to a mention/entity. It demonstrably *noticed*
   the token and still failed the subject→object generalization. Strong
   evidence the gap is in the mention-emission instruction, not in the model's
   ability to see the name.

Also note: robustness to the "are"/"ate" typo is incidental and fine; the
lapse is the dropped person, not the typo.
