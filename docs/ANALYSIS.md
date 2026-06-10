# JBrain2 — Note Analysis Pipeline

Binding reference for Phases 2–3 (and the Phase 6 wiki's inputs). Produced
from the owner's workflow concept plus a red-team and design review; owner
decisions are marked **[decided]**.

## The workflow

```
capture (Phase 1)
  → chunk + embed + FTS            (local, no LLM — searchable within seconds)
  → extraction call                (one strong-model structured call:
                                    title, tags, facts[], entity mentions,
                                    per-fact domain + temporal resolution)
  → domain split                   (mixed notes → per-domain derived chunks)
  → entity linking                 (alias/embedding match; one cheap LLM call
                                    only for the uncertain middle)
  → conflict detection             (candidate retrieval in SQL/pgvector; one
                                    cheap batched adjudication call if needed)
  → per-kind supersession          (+ review-inbox items)
nightly: entity hygiene, merge proposals, summary re-embedding,
         tag consolidation; (Phase 6) wiki triage
```

Capture-to-searchable never waits on a cloud LLM: embeddings/FTS index
immediately; facts and entities are async enrichment.

## Facts

A fact is a **semi-structured statement with a structural identity**, not a
free-text blob:

- Identity: `(subject, entity, predicate, qualifier)` — this is what makes
  "same fact, new value" detectable and re-extraction upsertable.
- `statement` (canonical one-sentence rendering — embedded, cited, shown),
  `value_json` for structured payloads (measurements: value + unit).
- `predicate` is free text plus the kind enum below — no controlled
  ontology **[decided]**; nightly consolidation merges near-duplicate
  predicates.
- Assertion status: `asserted | negated | hypothetical | reported |
  question` — the wiki demotes everything below `asserted`. "Doctor wants
  to rule out diabetes" is not a diabetes fact.
- Provenance: `note_id`, `chunk_id`, `extractor` (model id),
  `prompt_version`, `confidence`.

### Fact kinds and supersession **[decided: per-kind policy]**

| kind | example | temporal | conflict policy |
|---|---|---|---|
| `event` | "saw Dr. Patel June 3" | `valid_from` = occurrence | **never auto-supersede** — immutable; a conflict is an extraction error → review. The newest *mention* of an old event is usually the least precise. |
| `measurement` | BP 120/80, weight | instant + `value_json` | **never** — time-series, accumulate; same metric+time disagreeing → review |
| `state` | address, employer | `valid_from`/`valid_to` | newest-wins eagerly: close old interval (SCD-2), flag review. The old fact stays true *about its interval*. |
| `attribute` | birthday, blood type | timeless | **hold `pending_review`, never auto-supersede** — two birthdays is a bug, not news |
| `preference` | "prefers aisle seats" | from `reported_at` | newest-wins, low-urgency flag; superseded ones stay agent-visible |
| `relationship` | Bob —works_at→ Acme | interval | supersede only for functional predicates (small allowlist: employer, spouse…); default accumulate |

Supersession compares **fact validity time, never note capture time** — a
retrospective note about 2019 must not supersede the current address.
"Newest" = latest `reported_at` *among facts about the same validity
period*.

### Temporal model **[decided: always resolve to absolute]**

Bi-temporal: `valid_from`/`valid_to` (true in the world) vs `reported_at`
(= note capture time, client-side with timezone — the offline outbox means
server receipt time is wrong). Relative phrases are resolved at extraction
against the capture anchor and stored absolute with
`temporal_precision (instant|day|month|year|era|unknown)` plus the original
`temporal_phrase` for audit. "Last Tuesday" → a date; "when I was a kid" →
era-precision range; never store only-relative. Future-tense facts carry
`expected` status (they are not occurred events) and defer to the
appointments pipeline where applicable.

## Entities

- `entities` carry `kind`, `canonical_name`, summary + embedding, and
  **`subject_id`** when the entity is also a security subject — "Mom" the
  entity and Mom the subject are one identity; fact→subject attribution is
  a security field. Cross-*subject* misattribution is treated as a leak.
- Every link is span-anchored via `entity_mentions` (surface text + chunk +
  offsets), so merges are reversible: merge = tombstone
  (`merged_into_id`) + repoint, un-merge = re-resolve mentions.
- Auto-merge only on exact alias + same kind; everything else is a
  review-inbox proposal. Bare first names never auto-merge without
  co-mention signals. New entities are `provisional` until implicitly
  confirmed.
- Relational aliases ("Mom", "the Honda") live in `entity_aliases` —
  unambiguous in a single-owner corpus.
- **Domain placement [decided: inherit + promote]**: an entity inherits the
  domain of the note that created it; a later mention from a *less*
  restrictive domain proposes promotion via the review inbox. Facts always
  carry their own domains regardless of their entity's domain.

## Domains and the firewall

- Every fact, entity, mention, and derived chunk carries a domain and sits
  under the standard `has_domain_scope` RLS policy.
- **Mixed-domain notes [decided: split]**: analysis derives per-domain
  chunks from a mixed note; citations always point at a chunk in the
  *fact's own domain*, so no citation ever crosses the firewall and the
  RLS test for it is straightforward. The original note remains the source
  of truth in its capture domain; derived chunks reference their spans.
- Classification bias is asymmetric: misclassifying *into* health/finance
  is cheap; *out of* them is a leak. Domain can ratchet **up** without
  review, never down. Health/finance keywords block `general` assignment
  without review. Titles and tags are generated per-domain-content so a
  note list never leaks a sensitive auto-title.

## Privacy routing **[decided: cloud for everything, for now]**

All domains may use cloud LLMs (Anthropic/xAI) during development — recorded
as an explicit opt-in in config, not an accident. The LLM adapter's task
profiles carry a routing axis from day one so the end-state — **everything
local once a GPU lands** — is a config flip, not a refactor. Until then the
docs must not claim the domain firewall is a network-privacy boundary: the
adapter is the egress point. Intake-link subjects' data (Phase 7) re-raises
this decision explicitly before launch.

## Reprocessing and corrections

- Re-extraction (model/prompt upgrade) **upserts on the structural identity
  key**: same key → update rendering in place (citations survive); key gone
  → `retracted_by_reextraction` (not a conflict, no inbox noise); new key →
  insert. `prompt_version` makes corpus re-runs a planned, budgeted
  migration.
- **Human decisions are pinned overrides**: review-inbox resolutions,
  entity merges/rejections, domain corrections, and tag fixes survive any
  reprocessing; auto-supersession cannot override a pinned fact, only
  re-flag it.
- Doctrine split: *prose* (wiki) is corrected via correction notes;
  *structured pipeline outputs* (tags, domains, entity links, fact status)
  are corrected directly in the review inbox. A correction note's "elevated
  weight" is implemented as pinning the facts it asserts.
- Contested (flagged-and-unreviewed) facts are **held out of wiki builds**;
  the wiki never publishes an unreviewed supersession.
- The pipeline records per-note stage state so retries are idempotent and a
  worker crash never double-extracts (full engine arrives Phase 5; Phase 3
  ships the minimal watermark).

## Attachments

Extracted text joins the note's analysis, with guards: per-task token/cost
budget with a summarize-then-extract fallback for big documents; structured
medical/financial documents are *detected and routed* (deferred to the
Phase 7 typed parsers) rather than free-extracted into hundreds of facts;
facts derived from OCR carry reduced confidence, and low-confidence numeric
health facts never auto-supersede anything.

## Model routing & cost

| task | tier |
|---|---|
| `note.extract` (title+tags+facts+entities+temporal, one call) | strong |
| `entity.disambiguate` (batched, only uncertain mentions) | cheap |
| `fact.adjudicate` (batched, only retrieved candidates) | cheap |
| `correction_note.extract` | strong |
| embeddings | local container |

One guaranteed call + up to two conditional cheap calls per note; ~5–7k
tokens ≈ under $0.02/note. Conflict detection is bounded by candidate
retrieval (SQL identity match, else pgvector top-k scoped to same
entity+domain+kind) — never corpus-wide comparison. Concurrent offline-sync
bursts serialize per (entity, predicate) to keep supersession chains
deterministic. Over-extraction is the known quality risk: soft cap on
facts-per-note, honest confidence, review-inbox rejection rate as the
prompt-tuning signal.

## Review inbox integration

One generic `review_items` queue (already designed) absorbs: fact
conflicts, attribute collisions, entity-merge proposals, ambiguous
mentions, domain promotions/demotions, low-confidence extractions.
Resolutions write pinned overrides and, where the fix is prose-shaped,
draft correction notes.
