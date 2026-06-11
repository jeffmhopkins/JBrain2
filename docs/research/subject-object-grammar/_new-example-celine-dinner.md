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

## Separate bug in the same screenshot — "last night" resolved off by one

Distinct failure class (temporal, not subject-object), logged here so it
isn't lost:

- **Capture anchor:** Jun 11 2026 · 7:13 AM. "last night" should resolve to
  the evening of **Jun 10**. The Analysis DATES token shows **Jun 11 → Jun 11**
  — the capture day, off by one.
- **Pure model/prompt lapse.** prompt.py:98-103 asks the model to resolve every
  relative phrase against the anchor to an absolute value itself; it returned
  the capture day instead of the prior evening.
- **No deterministic net.** extraction.py only post-corrects *future* dates
  (`normalize_future_assertion`: future "asserted" -> "expected"). A backward
  phrase landing on the wrong day passes through untouched. Candidate for a
  separate temporal-resolution investigation.
