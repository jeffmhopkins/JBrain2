# Predicate canonicalization (embedding-assisted) + typed value shapes

Status: **proposal** (design + plan; not yet built). Owner-facing problem,
machine-facing fix. Read alongside `docs/ANALYSIS.md` (Facts, the open
vocabulary), `docs/entity.md` (the soft schema registry and its *deferred*
consumers), and `tests/eval/README.md` (the gate that surfaced this).

## 1. The problem

The note→graph pipeline lets the model choose predicate spellings. Against real
Grok this vocabulary is **non-deterministic**: the same concept arrives as
`medication` / `prescription` / `takes`, `mortgageServicer` /
`mortgageServicingProvider`, `glucose` / `bloodGlucose`, `account` / `opened`.

Today `normalize_predicate` only collapses spellings a human pre-listed in a
predicate's `renamed_from`. An *unseen* spelling passes through **unchanged and
unmerged** (`schema/models.py:124`), so one real-world concept fragments into
several graph addresses. The owner sees duplicate-ish predicates; supersession
and the firewall floor (which key on the predicate) misfire; and the eval gate
cannot pin a predicate without becoming flaky.

Evidence: the DB-mode eval calibration (PR #166) had to *drop* every
predicate-pinned assertion and fall back to asserting only the domain floor,
because the predicate Grok emitted varied run to run. `mixed-domain-journal`
could not be hard-gated at all — its health floor depends on Grok happening to
use a registry-recognized health predicate.

## 2. What already exists (so this is mostly "finish the deferred design")

The schema registry is richer than the runtime uses:

- **Per-predicate metadata** is already declared in `schema/defs/**.yaml` and
  loaded into `schema/models.py:Predicate`: `canonical_name`, `value_shape`
  (`scalar|text|enum|quantity|date|ref|structured`), `kind`, `functional`,
  `enum_values`, `range_type`, `shape`, `renamed_from`, `description`. The
  **typed value table the proposal wants largely exists already** — it is just
  not enforced.
- **`docs/entity.md` explicitly marks value-shape validation as deferred**: "the
  data they would read (`value_shape`, `enum_values`, …) is already in the YAML
  and loader-validated, so building them later is a small change."
- **The normalization hook is a single function** (`SchemaRegistry.normalize_predicate`,
  `schema/models.py:124`) called at exactly the right points: `extraction.py`
  `parse_extraction`, `intent_parse.py` `_parse_fact` and `_parse_supersession`,
  and `consolidation.py` `plan_renames`.
- **`predicate_known`** already drives a weight penalty (`arbiter.py:179`,
  `weight.py`): it checks whether *any* entity type declares the predicate.
- **Drift mismatch to fix in passing**: `is_functional` reads a hardcoded
  allowlist (`supersession.py:24`) instead of the registry's `functional` flag —
  so a `functional: true` predicate in YAML is ignored by supersession unless it
  is also in the hardcoded set.
- **Embedding infrastructure is ready to reuse**: `EmbedClient.embed`
  (`embed.py:27`, TEI, 384-dim bge-small), `vector_literal`, pgvector with HNSW
  `vector_cosine_ops` indexes, and the cosine-search template
  `1 - (embedding <=> cast(:v AS vector))` with calibrated bands
  (`_EMBED_STRONG=0.90`, `_EMBED_WEAK=0.78`) in `analysis/entities.py:448`.

So the net-new work is: an embedding index over canonical predicates, the
similarity decision in the normalization path, and turning on the
already-declared value-shape validation.

## 3. Design

### 3.1 The canonicalization decision (the hook)

Extend the normalization path so an **unknown** predicate is matched by meaning,
not just by pre-listed spelling:

```
raw predicate
  └─ registry.normalize_predicate(raw)            # exact/alias (synonym table)
       ├─ hit  → canonical                         # zero cost, unchanged
       └─ miss → embed(descriptor(raw)) and cosine-search the predicate index
                  ├─ sim ≥ STRONG        → canonicalize to the match
                  ├─ WEAK ≤ sim < STRONG → accept raw + file a predicate-proposal review card
                  └─ sim < WEAK          → MINT a new canonical predicate (self-extending), index it
```

Bands mirror entity resolution (reuse, then re-calibrate via eval). The synonym
table stays the fast path; embedding is touched **only on a miss**, so the steady
state (known predicates) costs nothing.

Critically, **embed a descriptor, not the bare token.** Short predicate strings
embed poorly — `worksFor` and `worksWith` are lexically close but opposite in
meaning. The index row stores a short definition + example, and we embed *that*;
for the incoming raw predicate we embed `raw + " " + sample_statement` (the
fact's statement gives the model's intended meaning). This is the single biggest
quality lever and the main thing the eval must validate.

### 3.2 The typed value table (turn on the deferred consumer)

Every canonical predicate already declares a `value_shape`. Add the validator
the docs deferred:

- At parse time (`_parse_fact`), after canonicalization, look up the predicate's
  `value_shape` and validate/coerce `value_json`:
  - `ref(<type>)` → expect an `object_entity_id` (an edge), not a scalar value.
  - `date` → a temporal token / ISO date.
  - `quantity` → `{value, unit}` (the shape already in `_meta.yaml`).
  - `enum` → a member of `enum_values`.
  - `scalar`/`text` → a bare datum.
- Per the storage invariant (`entity.md`: "predicate-name validation may never
  reject anything; shape validation *may* reject a malformed `value_json`"), a
  shape mismatch does **not** drop the fact — it routes to **review** (a
  shape-mismatch card) or coerces, never silently corrupts.

This is what makes the firewall floor and supersession deterministic: once a
health concept always lands on the same canonical health predicate with the
declared shape, `domain_code` floors the same way every run.

### 3.3 Storage

A `canonical_predicates` reference table, seeded from the registry and extended
at runtime:

| column | notes |
|---|---|
| `canonical_name` | PK; the registry canonical (e.g. `name.legal`) |
| `descriptor` | the text we embed (definition + example) |
| `embedding` | `vector(384)`, HNSW `vector_cosine_ops` |
| `embedding_model` | provenance, like chunks/entities |
| `value_shape`, `kind`, `functional` | mirrored from the registry for runtime reads |
| `origin` | `seed` (from YAML) \| `minted` (embedding cold-miss) |
| `created_at` | minted-at, for review/sweeps |

It is **global reference data** (predicates are not domain-scoped), so it follows
the `app.domains` precedent (`migrations/0001`, `domains_read … USING (true)`)
**except** it is self-extending, so the pipeline (SYSTEM_CTX) needs INSERT. That
means a real RLS policy (global SELECT; INSERT restricted to the owner/system
context) and — per CLAUDE.md rule 3 — a new RLS isolation test.

The hardcoded `FUNCTIONAL_PREDICATES` set collapses into this table's
`functional` column (fixing the drift in §2).

### 3.4 Seeding / bootstrap

A migration (or idempotent startup task) embeds every registry predicate's
descriptor once and inserts the `seed` rows. Re-runnable: skip rows whose
`embedding_model` matches the current model; re-embed on model change (same
backfill discipline as entities/chunks).

## 4. Drift control (the real risk)

A self-minting vocabulary can sprawl. Mitigations:

- **High STRONG threshold** so near-duplicates merge, low-confidence mints are
  rare; tune on the eval, not by guess.
- **Mints are visible**: `origin='minted'` + a review card, so the owner/agent
  can confirm-merge or rename. This is squarely `docs/ASSISTANT.md`
  self-improvement territory — the agent periodically reviews minted predicates
  and proposes merges back into the registry YAML (a correction note, never a
  silent edit — CLAUDE.md rule 7).
- **A minted predicate's value-type** is inferred from its first `value_json`
  shape and held for review, not trusted blindly.
- **Consolidation already exists** (`consolidation.py plan_renames`): once a
  mint is confirmed-merged, the nightly sweep rewrites stored drift rows to the
  canonical — so history heals, not just new writes.

## 5. How we know it works (eval)

This is exactly what the DB-mode harness (PR #166) is for. With canonicalization
on:

- Re-pin the predicates in the firewall corpus cases (the ones DB-mode had to
  drop to domain-floor-only) and confirm they pass deterministically across
  repeated real-Grok runs.
- Flip `mixed-domain-journal` to a hard gate (its health floor becomes
  deterministic once `medication`/`prescription`/`epiPen` collapse to one
  canonical health predicate).
- Add cases that feed deliberate drift spellings and assert they canonicalize to
  the expected predicate (STRONG band), that a novel concept mints exactly one
  new predicate (cold band), and that a near-miss files a proposal (WEAK band).
- Calibrate STRONG/WEAK against this corpus the same way entity bands were.

## 6. Phased plan

1. **Typed value-shape validation (no embeddings).** Turn on the deferred
   consumer: validate `value_json` against the predicate's declared `value_shape`
   at `_parse_fact`, routing mismatches to review. Make `is_functional`
   registry-driven; fold the hardcoded set into YAML. *Pure registry work, no new
   table — independently valuable and the safest first step.* Tests: unit
   validation per shape; an integration test that a shape mismatch holds for
   review instead of corrupting.
2. **`canonical_predicates` table + seed.** Migration (table, HNSW index, RLS
   policy + isolation test), bootstrap embed of the registry vocab, reads wired
   for `value_shape`/`functional`.
3. **Embedding canonicalization in the hook.** The STRONG/WEAK/cold decision in
   the normalization path, behind a setting (default off). Descriptor embedding
   for both index rows and incoming predicates. Review cards for WEAK + minted.
4. **Eval calibration.** Drift/mint/near-miss corpus cases; tune bands; re-pin
   the firewall predicates and flip `mixed-domain-journal`; confirm stability
   over repeated runs. Enable the setting once green.
5. **Self-improvement loop.** Agent reviews minted predicates, proposes
   merges/renames into the registry YAML via correction notes; consolidation
   sweep heals stored drift.

## 7. Open questions

- **Descriptor quality**: is `raw + statement` enough context to embed the
  *intended* predicate, or do we need the entity kind too (`person.worksFor` vs
  `org.industry`)? The eval (§5) decides.
- **Index granularity**: one global predicate space, or per-entity-kind spaces
  (so `person.*` predicates don't match `appointment.*`)? Per-kind is cleaner
  but needs the kind at the hook; start global, split if the eval shows
  cross-kind collisions.
- **Mint authority**: does the *extractor* mint, or only the graph-aware
  *integrator* (which has more context)? Leaning integrator-only — fewer, better
  mints.
- **Cost**: one embed per *unknown* predicate per note. Known predicates
  short-circuit, so steady-state cost is ~zero; worth confirming on a real
  corpus.
