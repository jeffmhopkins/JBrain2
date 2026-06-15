# Fix Options: Issue 3 — Relative-time phrases resolve to the wrong absolute date

**Status:** Design only — no source files modified.  
**Observed failure:** Note "Jeff ate Celine's dinner last night", captured
2026-06-11 07:13 AM. The model resolved "last night" → Jun 11 (the capture
day) instead of Jun 10 (the prior evening). Off by one.  
**Root:** The prompt instructs the model to resolve every relative phrase
against the capture anchor (prompt.py lines 98-103), but provides no explicit
guidance about the backward-day-crossing semantics of night/evening phrases
captured in the small hours of the morning. The only post-extraction temporal
correction in the codebase (`normalize_future_assertion`, extraction.py:124-136)
exclusively guards *forward* mislabeling; no guard exists for backward phrases
landing on the wrong day.

---

## What the code actually does today

### Prompt instruction (prompt.py, PROMPT_VERSION = `"note-extract-v4"`)

```
"temporal": resolve every relative time phrase ("last Tuesday", "this
morning", "in 3 months") against the capture anchor given with the note, to
absolute ISO 8601 with UTC offset, preserving the anchor's local date. Set
"precision" honestly: instant | day | month | year | era | unknown. Never
invent dates: if a phrase cannot be resolved, keep the phrase and leave
resolved_start null. Use null temporal when the fact has no time dimension.
```

The anchor is passed as `anchor.isoformat()` in `build_user_prompt`
(prompt.py:248), so the model sees `2026-06-11T07:13:00-05:00` (or whatever
local offset). The instruction says "preserving the anchor's local date" but
does not say what "local date" means for phrases that cross midnight backward.

### Existing post-correction (extraction.py:124-136)

`normalize_future_assertion` fires when `resolved_start > anchor` and
`assertion == "asserted"` → flips to `"expected"`. This is the sole temporal
correctness guard in the pipeline. It has no analog for backward phrases.

### Token dropping rule (extraction.py:392-397)

A temporal token whose `resolved_start` parses to `None` is dropped with a
warning log. This means a wrong-day resolution (e.g., Jun 11 instead of Jun
10) passes through silently — the pipeline has no way to distinguish a
correct resolution from an off-by-one one without knowing what the phrase
*was*.

### Harness constraint

The harness in `backend/tests/harness/README.md` explicitly states: "It tests
the **deterministic pipeline given good model output**. It does *not* test the
prompt — only a live model exercises that." The hist_* scenarios
(`hist_dst_boundary_local_day`, `hist_midnight_straddle_reported_at_tiebreak`,
`hist_out_of_order_outbox`, etc.) all use hand-authored resolutions and pin
*pipeline* behaviour. A temporal-resolution error is a model/prompt accuracy
problem that these scenarios cannot catch, no matter how many are added.

---

## Option 1 — Prompt engineering only

### How it works

Tighten the existing temporal instruction block (prompt.py lines 98-103) with
three additions:

1. **Explicit anchor-crossing rule for night/evening phrases.** Add a
   sentence stating that if the capture anchor is in the early morning hours
   (e.g., before noon), "last night", "last evening", and "overnight" refer to
   the *prior calendar day*, not the anchor's calendar day. This is the exact
   failure mode observed.

2. **Worked examples for backward phrases.** The prompt already uses worked
   examples for facts (lines 114-132). Add a parallel worked example in the
   temporal block:

   ```
   Worked temporal examples (capture anchor 2026-06-11T07:13:00-05:00):
   - "last night"    → range on 2026-06-10 evening  (resolved_start "2026-06-10T20:00:00-05:00", precision "day")
   - "this morning"  → earlier today                (resolved_start "2026-06-11T07:13:00-05:00",  precision "day")
   - "yesterday"     → prior calendar day           (resolved_start "2026-06-10T00:00:00-05:00",  precision "day")
   - "last Tuesday"  → the most recent Tuesday before the anchor's local date
   - "a week ago"    → 7 days before the anchor's local date
   ```

3. **Explicit disambiguation for ambiguous weekday references.** "Last
   Tuesday" captured on a Wednesday should resolve to the *immediately prior*
   Tuesday (6 days back), not the Tuesday two weeks ago. State this explicitly.

**Proposed snippet shape (replacing lines 98-103 in SYSTEM_PROMPT):**

```python
'- "temporal": resolve every relative time phrase ("last Tuesday", '
'"this morning", "in 3 months") against the capture anchor given with '
'the note, to absolute ISO 8601 with UTC offset, preserving the '
'anchor\'s local timezone offset. Night/evening phrases ("last night", '
'"last evening", "overnight") captured BEFORE NOON on day D refer to '
'the EVENING OF DAY D-1 — not day D itself. "This morning" captured '
'before noon refers to the same calendar day as the anchor. '
'"Yesterday" and "last <weekday>" always resolve to dates before the '
'anchor\'s local calendar day. "Last <weekday>" resolves to the most '
'recent occurrence of that weekday strictly before the anchor date '
'(never the anchor day itself). Set "precision" honestly: instant | '
'day | month | year | era | unknown. Never invent dates: if a phrase '
'cannot be resolved, keep the phrase and leave resolved_start null. '
'Use null temporal when the fact has no time dimension.\n'
'Worked temporal examples (anchor 2026-06-11T07:13:00-05:00):\n'
'  "last night"   → resolved_start "2026-06-10T20:00:00-05:00", '
'resolved_end "2026-06-11T00:00:00-05:00", precision "day"\n'
'  "this morning" → resolved_start "2026-06-11T07:13:00-05:00", '
'resolved_end null, precision "day"\n'
'  "yesterday"    → resolved_start "2026-06-10T00:00:00-05:00", '
'resolved_end null, precision "day"\n'
'  "last Tuesday" → resolved_start "2026-06-09T00:00:00-05:00", '
'resolved_end null, precision "day"'
```

**Files touched:** `backend/src/jbrain/analysis/prompt.py` (SYSTEM_PROMPT
block + PROMPT_VERSION bump to `"note-extract-v5"`).

**PROMPT_VERSION discipline:** A bump from `note-extract-v4` to
`note-extract-v5` triggers a corpus re-extraction migration for all notes
previously analyzed under v4. docs/ANALYSIS.md states: "Re-extraction upserts
on the structural identity key … `prompt_version` makes corpus re-runs a
planned, budgeted migration." This is not free — every note in the corpus gets
one strong-model call. At ~$0.01/note (grok-4.3 rates from ANALYSIS.md) this
is manageable but must be budgeted.

### Pros

- Minimal code delta; pure text change.
- Directly addresses the root cause at the source.
- The worked examples serve as in-context few-shot demonstrations, which
  LLMs respond to reliably.
- No new dependencies.
- No change to the pipeline's data model.

### Cons

- **Not CI-testable.** The harness cannot validate prompt changes without a
  live model call. A human must run a live eval to verify improvement.
- **Probabilistic, not deterministic.** Even with better instructions, a model
  can lapse on novel phrasing or unusual anchors (e.g., midnight capture with
  "last night").
- **No catch net.** A mislabeled temporal still passes through the pipeline
  silently. The only feedback is downstream inconsistency (a review-inbox
  conflict if another note covers the same interval) or human discovery.
- The "before noon" heuristic is a crude proxy; early-morning anchors near
  midnight (00:30) are edge cases the rule handles but the worked example
  doesn't cover. A model reasoning from the example alone may not generalize
  to 00:30.
- Requires corpus re-migration on version bump.

### Interaction with normalize_future_assertion

None — this option does not change or extend the normalize function. The
existing forward guard and this new backward guidance are orthogonal
instructions.

### Effort: S

One file changed. The work is writing and testing the examples.

---

## Option 2a — Deterministic post-hoc validator / repairer

### How it works

Add a new function `validate_backward_temporal(fact, anchor)` (or
`repair_backward_temporal`) in `extraction.py`, modeled directly on
`normalize_future_assertion`, that:

1. Inspects `fact.temporal.phrase` for a **closed set of backward-pointing
   patterns** ("last night", "last evening", "overnight", "yesterday",
   "last <weekday>", "a week ago", "two days ago", etc.) using a
   case-insensitive regex or a small phrase table.
2. Computes the **expected resolved_start** for that phrase from the capture
   anchor using only the Python standard library (`datetime` + `timedelta` +
   `calendar.weekday`). No third-party library needed for this closed set.
3. If the model's `resolved_start` does not match the computed value (within
   the phrase's expected precision — a full calendar-day tolerance for
   day-precision phrases), the function either:
   - **(Repair variant):** replaces `resolved_start` (and, for range phrases
     like "last night", `resolved_end`) in the returned `ExtractedTemporal`
     with the deterministically computed value, and logs a
     `analysis.temporal_repaired` structured warning.
   - **(Flag variant):** leaves `resolved_start` unchanged but lowers
     `fact.confidence` below a threshold and sets a flag for review routing
     (see Option 3 for the routing side).

The function is called in the `parse_extraction` loop immediately after
`normalize_future_assertion`, maintaining the same pipeline position and
pattern.

**Key phrase table (illustrative; not exhaustive):**

| Phrase pattern | Resolution rule |
|---|---|
| `last night` / `last evening` | prior calendar day, ~20:00 anchor local offset, precision `day` |
| `overnight` | prior calendar day → anchor day, precision `day` |
| `yesterday` | anchor date − 1 day, start of day, precision `day` |
| `this morning` | anchor's calendar day, precision `day` |
| `last <weekday>` | most recent occurrence of weekday strictly before anchor date, precision `day` |
| `a/one/two/… week(s) ago` | anchor − N×7 days, precision `day` |
| `N days ago` | anchor − N days, precision `day` |
| `last month` | first day of the month before the anchor's month, precision `month` |
| `last year` | January 1 of anchor year − 1, precision `year` |

Phrases NOT in this table (e.g., "the other week", "a while back", "recently")
are left to the model; no deterministic resolution is attempted.

**DST / timezone correctness:** The `hist_dst_boundary_local_day` scenario
pins that temporal arithmetic must use the anchor's local offset, not UTC.
The resolution must compute `anchor_local_date = anchor.astimezone(anchor.tzinfo).date()`
and build the result datetime with the same tzinfo. The existing
`parse_datetime` in `extraction.py:96-106` pins naive output to UTC (model
slop) — the validator must use the anchor's actual offset, not UTC. This is
the same lesson the DST scenario encodes.

**Files touched:**
- `backend/src/jbrain/analysis/extraction.py` — add
  `_resolve_backward_phrase(phrase, anchor)` helper and
  `validate_backward_temporal(fact, anchor)` post-step; call site after
  `normalize_future_assertion` in `parse_extraction`.
- `backend/tests/unit/test_extraction.py` (or a new
  `test_temporal_resolution.py`) — unit tests for every phrase in the table,
  covering at least: early-morning anchor ("last night" at 07:13), late-night
  anchor ("last night" at 23:50 — a different edge case), DST boundary
  anchors (using the same dates as `hist_dst_boundary_local_day`), and the
  "yesterday" / "last Tuesday" weekday cases.
- `backend/src/jbrain/analysis/prompt.py` — PROMPT_VERSION bump still needed
  if prompt changes accompany this (see hybrid recommendation).

**No new external dependency** for the closed phrase set. If scope expands to
open-ended relative phrases, `python-dateutil` (already likely in the venv
or trivially addable) offers `relativedelta` parsing; `duckling` (Haskell
service) is overkill for this corpus size and adds operational complexity.

### Pros

- **Fully CI-testable.** Unit tests for `validate_backward_temporal` exercise
  every phrase in the table deterministically — no live model needed. This
  directly addresses the harness gap identified in the README.
- **Deterministic correctness** for the closed set. A model lapse on "last
  night" is always caught and repaired, regardless of prompt version.
- Mirrors the exact pattern of `normalize_future_assertion` — low cognitive
  overhead for future maintainers.
- Structured log (`analysis.temporal_repaired`) gives an observable signal for
  how often the model is miscalibrated; this is the prompt-tuning signal
  analogous to the review-inbox rejection rate.
- Timezone/DST correctness is enforced in code and covered by tests, not
  left to a model that may not read timezone instructions carefully.
- No corpus re-migration required if deployed alone (no PROMPT_VERSION bump
  needed — post-extraction repair does not change the extraction schema or
  identity keys; it only corrects values before storage).

### Cons

- The phrase table is a **closed set** — novel phrasing ("the night before
  last", "two evenings ago") falls through to the model unchecked. The table
  must be maintained.
- **Repair-variant risk:** Silently overwriting the model's output hides
  cases where the phrase is genuinely ambiguous or where the model had
  additional context justifying its resolution. The flag-variant preserves
  transparency at the cost of needing a review mechanism.
- The "last night at 07:13 anchor" rule requires a heuristic ("before noon
  means prior day") that is correct in the observed case but could be wrong
  for cultural or personal usage ("last night" said at 08:00 PM could mean
  the previous night). The model's contextual understanding of the *full note*
  can sometimes outperform a rule-based resolver.
- Adds a small amount of extraction.py complexity; the function must be kept
  in sync with the phrase table as it grows.

### Effort: M

New function + unit test suite. The test suite is the majority of the work
(covering every phrase variant, multiple anchor times, DST boundaries).

---

## Option 2b — Fully deterministic resolution for a wider set (library-based)

### How it works

Replace Option 2a's hand-rolled phrase table with a call to a proper
relative-time parsing library before or instead of the model for temporal
tokens. Two sub-options:

- **`python-dateutil` + `dateparser`:** `dateparser.parse(phrase, settings={"RELATIVE_BASE": anchor, "RETURN_AS_TIMEZONE_AWARE": True, "PREFER_DAY_OF_MONTH": "first"})` handles a wide variety of natural-language time expressions. Pure Python, no service required.
- **Duckling (Facebook's Haskell NLP service):** Industry-standard for temporal
  NER and relative-time resolution; very high coverage; but requires running
  a separate service, adds operational complexity, and is overkill for a
  personal-scale system.

The pipeline would run the library-based resolver against `temporal.phrase`
(the original phrase retained in the schema for audit — exactly as designed
in ANALYSIS.md: "the original `temporal_phrase` is retained for audit") and
compare or replace the model's resolved value.

**Files touched:** Same as 2a, plus a new dependency
(`dateparser` or `duckling-client`); `scripts/dev-setup.sh` must be updated
in the same PR (CLAUDE.md non-negotiable 8).

### Pros

- Covers a much wider surface area than a hand-rolled table.
- `dateparser` is battle-tested for English relative expressions.

### Cons

- **`dateparser` has known quirks** with ambiguous phrases and cultural
  settings that are hard to predict without extensive testing. The library
  needs timezone-aware base anchoring carefully configured — a misconfigured
  RELATIVE_BASE is as wrong as a model lapse.
- Adds a production dependency; `scripts/dev-setup.sh` update required.
- The library's behavior on novel phrases may be less predictable than the
  model's, which has broad contextual reasoning ability.
- Duckling adds a service and operational overhead completely out of scale for
  a personal knowledge system.
- For the *actual* failure mode (a handful of backward time phrases), this is
  heavy machinery for a narrow problem.

**Verdict:** Option 2b is not recommended for the initial fix. It is the right
long-term evolution *if* the phrase table in 2a grows large and brittle. Start
with 2a.

### Effort: M-L

Same as 2a plus library evaluation, configuration, and a new dependency in
the project's dependency lockfile and dev-setup.

---

## Option 3 — Confidence-based review routing

### How it works

Rather than (or in addition to) repairing temporal values, route facts with
suspect temporal resolutions to the review inbox. The review inbox is already
designed as the general safety valve for uncertain pipeline outputs
(ANALYSIS.md: "One generic `review_items` queue absorbs: fact conflicts,
attribute collisions, entity-merge proposals, ambiguous mentions, domain
promotions/demotions, low-confidence extractions").

**Trigger signals:**

1. **Phrase-category mismatch:** A backward phrase (in the closed set from
   Option 2a) where `resolved_start.date() >= anchor.date()` is a clear
   signal the model resolved forward when it should have resolved backward.
   This is the exact condition Option 2a detects; instead of (or in addition
   to) repairing, emit a `review_item` with `kind="temporal_resolution_suspect"`
   and the computed expected value as context.

2. **Precision mismatch:** A phrase like "last night" where the model returned
   `precision="instant"` (a specific time) rather than `precision="day"` (an
   approximate interval) may indicate the model invented a time rather than
   estimated one.

3. **Explicit model-reported low confidence:** The model already emits a
   `confidence` float per fact (0–1). A fact with `confidence < 0.5` whose
   temporal resolves a relative phrase is already a candidate for review.
   Adding a specific `temporal_confidence` field to the schema (a schema
   change, not just a prompt change) would give the model a more precise
   signaling channel, but that is a larger change.

**Files touched:**
- `backend/src/jbrain/analysis/extraction.py` — add review-item creation
  alongside (not instead of) the mismatch detection from Option 2a.
- The review_items write path (wherever `review_items` are persisted —
  search needed for the exact module; likely in the pipeline orchestrator
  or the supersession layer).
- `backend/tests/` — tests that a detected mismatch produces the expected
  review item.

### Pros

- Transparent: wrong resolutions surface to the user rather than being
  silently stored or silently repaired.
- Leverages an already-designed review infrastructure.
- Combinable with Option 2a: repair the clear-cut cases, route the ambiguous
  ones.

### Cons

- **Review fatigue.** For a personal knowledge system processing many notes,
  a high false-positive routing rate would make the review inbox noisy. The
  signal must be high-precision; routing on "any backward phrase" would be
  too broad.
- Routing alone does not fix the stored value if the user doesn't review it.
  If the review inbox backlog grows, incorrect temporal values sit in the
  graph until cleared.
- Requires the review-item write path to be integrated; that path's exact
  location needs a code search to confirm it exists and is accessible from
  `extraction.py`.
- The `temporal_resolution_suspect` review kind is new and requires UI/inbox
  rendering support.

### Effort: M

Similar to Option 2a but includes the review-item write path, a new review
kind, and UI support.

---

## Option 4 — Verification / eval strategy

This option is not a standalone fix; it is the **verification layer** required
for any of options 1-3.

### The harness gap

The harness README is explicit: "It does *not* test the prompt — only a live
model exercises that." Every scenario in `backend/tests/harness/scenarios/`
uses hand-authored model outputs. There is no scenario that can fail because
the model resolved "last night" to the wrong day — the scenario authors
provide the resolved date directly.

This means Option 1 (prompt only) has **zero automated test coverage** for
its correctness claim. A prompt change that makes the problem worse would pass
the entire test suite.

### What each option can prove

| Option | Verification method | CI-testable? |
|---|---|---|
| 1 (prompt only) | Live model eval set — a small suite of notes with known "last night" / "yesterday" / "last Tuesday" phrases, run against the live model, assert `resolved_start` date. | No — requires live model, API key, cost. |
| 2a (deterministic validator) | Unit tests for `validate_backward_temporal` — parametrized over the phrase table, anchor times, and DST boundaries. These are pure Python, no DB, no model. | **Yes** — full CI. |
| 2b (library) | Same unit test structure, but also needs integration tests to validate `dateparser` behavior on corner cases. | **Yes** — full CI. |
| 3 (routing) | Unit tests that a mismatch generates the expected review item; integration test that the item lands in the DB. | **Yes** — full CI for the detection and routing logic (the underlying temporal arithmetic comes from 2a). |

### Recommended eval set (for Option 1 or to validate the hybrid)

A live-model eval set should cover:

| Note text | Anchor | Expected phrase resolution |
|---|---|---|
| "Jeff ate dinner last night" | 2026-06-11 07:13 -05:00 | last night → Jun 10 evening |
| "I took my meds last night" | 2026-06-11 23:45 -05:00 | last night → Jun 11 evening (same day) |
| "I went to bed last night at 11pm" | 2026-06-11 06:00 -05:00 | last night → Jun 10 |
| "I saw her yesterday" | 2026-06-11 14:00 -05:00 | yesterday → Jun 10 |
| "Last Tuesday the report came out" | 2026-06-11 10:00 -05:00 | last Tuesday → Jun 9 |
| "Last Tuesday's meeting" | 2026-06-10 10:00 -05:00 (Wednesday) | last Tuesday → Jun 9 |
| "A week ago I started the diet" | 2026-06-11 10:00 -05:00 | a week ago → Jun 4 |
| DST: "last night" | 2026-03-08 07:00 -06:00 (post-spring-forward) | last night → Mar 7, offset -07:00 |

**The CI argument for Option 2a is decisive.** The inability to automate
prompt verification is the strongest reason to include the deterministic
validator, even alongside prompt improvements. The validator converts a
"hope the model does it right" into a "we proved it in CI" for the closed
phrase set.

---

## Comparison matrix

| Dimension | Option 1 (prompt) | Option 2a (deterministic) | Option 3 (routing) | Hybrid 1+2a+3 |
|---|---|---|---|---|
| Fixes the root cause at source | Yes (probabilistic) | No (post-hoc) | No (catch net) | Yes |
| CI-testable | No | Yes | Yes | Yes (2a+3 parts) |
| Catches novel phrasing | Yes (model reasoning) | No (phrase table only) | Partial (routing signal) | Best coverage |
| Silent failures possible | Yes | No (for closed set) | Reduced | Minimal |
| Migration cost | Yes (PROMPT_VERSION bump) | No | No | Yes (one bump) |
| New dependency | No | No | No | No |
| Aligns with normalize_future_assertion pattern | N/A | Yes (direct analog) | Partial | Yes |
| Effort | S | M | M | M-L |
| DST safe | N/A | Yes (if code uses anchor offset) | Depends on 2a | Yes |

---

## Recommendation

**Implement a hybrid: Option 2a + Option 1, with Option 3 as a follow-on.**

### Phase 1 (primary fix — one PR)

1. **Add `validate_backward_temporal` to `extraction.py`** as a direct analog
   to `normalize_future_assertion`. Use the repair variant: when a backward
   phrase in the closed table resolves to a date on or after the anchor's
   calendar date, recompute from the anchor and replace, logging
   `analysis.temporal_repaired`. Phrase table covers at minimum: "last night",
   "last evening", "overnight", "yesterday", "this morning", "last <weekday>",
   "N days/weeks ago", "last month", "last year".
   - **DST correctness:** Compute dates using `anchor.astimezone(anchor.tzinfo).date()`
     and reconstruct with the anchor's offset. This follows the same lesson as
     `hist_dst_boundary_local_day`.
   - **Call site:** Immediately after `normalize_future_assertion` in
     `parse_extraction`, applied per-fact. Also apply to temporal_tokens in
     their own loop.
   - **No PROMPT_VERSION bump needed** for 2a alone — this is a pipeline
     post-correction, not a schema or prompt change.

2. **Add a unit test suite** (`test_temporal_resolution.py` or extend
   `test_extraction.py`) covering the full phrase table, multiple anchor times
   (early morning, late night, midnight edge cases), and DST boundary anchors.
   All tests are pure Python, fully CI-able.

### Phase 2 (prompt improvement — can be same PR or separate)

3. **Tighten the temporal instruction block in `prompt.py`** with the explicit
   anchor-crossing rule and worked examples from Option 1. Bump
   `PROMPT_VERSION` to `"note-extract-v5"`. Budget a corpus re-migration.
   - The deterministic validator from Phase 1 means the prompt improvement is
     now **defense in depth** rather than the sole fix. Even if the model
     lapses on the new wording, the validator catches and repairs.
   - Together, the prompt change reduces how often the validator has to fire,
     and the `analysis.temporal_repaired` log gives visibility into residual
     model miscalibration.

### Phase 3 (optional hardening)

4. **Route unresolvable backward phrases to the review inbox** (Option 3)
   for phrases NOT in the closed table where `resolved_start >= anchor`. This
   handles novel phrasing the model may also get wrong and the validator
   cannot fix. Low priority; implement only if the `analysis.temporal_repaired`
   log shows a tail of unrepairable misresolutions in production.

### Why not Option 2a alone without the prompt fix?

The validator repairs after the fact; it does not improve the model's
accuracy on phrases outside the closed set. Prompt improvements are cheap and
compound the gain. The PROMPT_VERSION migration cost is the only price, and it
is paid once.

### Why not Option 1 alone?

Because it produces zero new CI coverage for a correctness claim. The harness
README is explicit that prompt changes cannot be validated by the harness; a
regression in prompt behavior would pass the full test suite undetected. The
deterministic validator converts temporal correctness for the common phrase
set from untestable to tested.

### CLAUDE.md non-negotiable alignment

- **Non-negotiable 1** (LLM only via adapter): not implicated — no LLM calls
  added.
- **Non-negotiable 2** (storage abstraction): not implicated — no new file I/O.
- **Non-negotiable 3** (RLS-scoped DB): not implicated.
- **Non-negotiable 5** (tests same PR, 80% backend coverage): Phase 1's
  `test_temporal_resolution.py` satisfies this. Phase 2's prompt change needs
  a live-model eval outside CI to validate, which is acceptable given Phase 1
  provides the deterministic safety net.
- **Non-negotiable 6** (Conventional Commits/PR): one PR per phase;
  `fix(analysis): repair backward-time-phrase resolution off-by-one` for
  Phase 1; `fix(analysis): tighten temporal anchor-crossing instructions`
  for Phase 2.
- **Non-negotiable 8** (dev-setup.sh): not implicated if no new dependency
  added (pure stdlib for 2a).
