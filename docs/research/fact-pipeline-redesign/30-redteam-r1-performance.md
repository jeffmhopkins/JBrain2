# Red-team R1 — Performance & Scale lens

**Spec under attack:** `20-spec-v0.md` (synthesis v0).
**Lens:** performance & cost at realistic scale. Target scale assumed: ~10 years of
notes, **10^4–10^5 live facts**, **10^5–10^6 assertion rows** (append-only history
multiplies the live set by the average revision count), thousands of entities each
with up to 4 domain projections, and recurring-event facts in the hundreds.
**Method:** trace each hot path, give Big-O / row-count / round-trip cost, name where
it falls over and the specific index/materialization/cache that saves it.

A note on the dominant cost model: this is a **single-owner** system, so there is no
multi-tenant fan-out — but "single owner over 10 years" still means the *entire*
corpus lives behind one `owner_id`, so every per-owner index degenerates toward a
full-table structure and selectivity comes almost entirely from `(subject, predicate,
domain, slot_key)`. Indexes that lead with `owner_id` or `domain_id` alone (F §4.5
mandates `domain_id`-leading composites) are **near-useless for selectivity** at this
scale — they partition 10^6 rows into ~4 domain buckets. This compounds several
findings below.

---

## SEV-1 findings (will not scale to target without a design change)

### S1-1 · Current-value derived at read time over an append-only log has no bounded query plan
**Severity: SEV-1**

**Hot path.** Every read surface — wiki render, review fat-read (§4.3), entity card,
"current employer", current-value for supersession (G §4) — must compute *current
value* from `fact_assertion`. The spec makes the slot a **derived key, not a table**
(B §1.1, §3.3, restated §3 of spec: "Not a stored table by default") and current-value
a **computed** quantity (G C3, §4). The canonical read is:

```
SELECT * FROM fact_assertion
WHERE owner_id=? AND tx_to IS NULL AND state='live'
  AND valid-time gate AND (subject,predicate,domain filter)
```

then a per-slot `argmax(valid_from_sortkey, reported_at, confidence, recorded_at)`
collapse for functional predicates (G §4).

**Cost reasoning.** The partial unique index `one_live_per_slot ... WHERE tx_to IS
NULL AND state='live'` (B §3.3) keeps the *live* set to one row per slot, so a
**point** lookup of one slot is fine: O(log n) on `slot_key`. The problem is the
**non-point** reads that dominate real workloads:

- **"Render everything about entity X"** (entity card, wiki article): filter is
  `subject_id = X`, not `slot_key`. There is no slot_key for "all of X". This scans
  the live partial index filtered by subject. If `slot_key` is the leading index
  column (it is, B §3.3), the planner **cannot** use it for a `subject_id` predicate
  — it falls back to a separate `(subject_id) WHERE tx_to IS NULL` index or a bitmap
  scan over the live set. That index is **not in the spec**. Without it: scan of the
  whole live partial index (~10^4–10^5 rows) per article build.
- **Wiki full rebuild / reprocess** touches every entity: O(entities × avg-facts) =
  O(live set) per pass, but the *valid-time + supersession collapse* is done in
  application code per slot, not in SQL. The `argmax` over `(valid_from_sortkey,
  reported_at, confidence, recorded_at)` is a 4-key sort the GENERATED `valid_range`
  column does **not** support — `valid_from_sortkey` is precision-aware (G §4:
  start-of-window vs end-of-window, year `2026` vs `2026-06`) and therefore **not a
  plain column comparison**. So the collapse cannot be pushed into an index; it is a
  per-slot application-side reduction.

**Where it falls over.** The "derived, never stored" stance (B §5.4 makes
materialization "an optional cache") is exactly backwards for a read-heavy
wiki/knowledge system. Reads vastly outnumber writes here (one note ingest → many
article renders, search hits, review loads). Deriving current-value from a 10^6-row
append-only log on *every render* is the classic event-sourcing read-amplification
trap. B's own open-Q 3 ("at what corpus size does the partial-index live-view stop
being fast enough") concedes this is unanswered — the spec ships the slow path as the
default and the fast path as "optional."

**Mitigation (required, not optional).**
1. **Materialize the slot/current-value as a maintained table** (`fact_slot` or
   `fact_current`), updated transactionally by the committer in the same op
   transaction (it already holds the advisory lock per slot, B risk 4). This makes
   current-value an O(1) point/range read and removes the per-slot app-side `argmax`
   from the hot path. Treat it as **authoritative-cache** (rebuildable from
   assertions for audit), not optional. Promote B §5.4 from "optional optimization"
   to "baseline."
2. Add the missing **`(owner_id, subject_id, domain_id) WHERE tx_to IS NULL AND
   state='live'`** index for entity-centric reads, and a
   **`(owner_id, slot_key) WHERE tx_to IS NULL AND state='live'`** for slot reads.
3. The precision-aware `valid_from_sortkey` must be **precomputed into a sortable
   column** at write time (a `timestamptz` normalized to start/end-of-window + a
   tiebreak ordinal), or every current-value collapse is an application sort that
   defeats the index.

---

### S2-1 · Two-stage extraction is **per-candidate**, so cost is O(facts), not O(notes) — plus a per-candidate embedding/retrieval query
**Severity: SEV-1**

**Hot path.** D §1.1 / spec §5: Stage 1 is one call per note; **Stage 2 is one
constrained LLM call per candidate fact** ("STAGE 2: TYPE + LINK (per candidate)").
For each Stage-2 call the committer must *inject* (a) the **canonical predicate slice
by embedding nearness to the predicate phrase** and (b) **entity candidates for
subject and object** (D §2.3 CONTEXT block). Predicate canonicalization (C2) and
coined-slug dedup also run an embedding lookup per candidate.

**Cost reasoning.** Let a note yield `k` candidate facts (commonly 3–15 for a
substantive note). Per note:
- LLM calls = `1 + k` (not 2). Over a corpus the LLM bill and wall-clock scale with
  **total facts**, not notes. The spec's own §6 mitigation ("stage 2 batches
  candidates") is in tension with the per-candidate **dynamic context injection**:
  each candidate needs a *different* predicate slice and *different* entity
  candidates, so they cannot share a prompt prefix cleanly and batching is limited.
- Embedding/ANN queries per note = `1 (predicate) + 2 (subj+obj entity retrieval)`
  **× k candidates** = `3k` vector searches, each over a growing entity/predicate
  index. At 10^4 entities the ANN is cheap individually (O(log n)-ish with HNSW) but
  `3k` per note × every reprocess pass is the real multiplier.
- **Entity-candidate retrieval recall is the silent killer** (D open-Q 7): if
  retrieval misses the right entity, the model is forced to `mint` a duplicate, which
  (a) pollutes the graph and (b) makes the *next* resolution slower because the entity
  set grew. Recall degrades precisely as the corpus grows — more near-duplicate
  surfaces ("Sam", "Sam B.", "Samuel") per query — so candidate lists must grow to
  hold recall, which enlarges every Stage-2 prompt (more tokens per the `3k` calls).

**Where it falls over.** A **major-version re-analysis migration** (D §4.2) re-runs
Stage 1 + Stage 2 over the *whole corpus*: `O(total candidates)` LLM calls +
`O(3 × total candidates)` vector queries. At 10^5 facts that is ~10^5 LLM round-trips
and ~3×10^5 ANN queries in one job. D §4.2's "budget gate" makes the cost *visible*
but does not make it *affordable* or *fast* — a major bump is effectively a
re-ingest of the entire knowledge base. The spec treats major migrations as routine
("plan→budget→shadow+diff→cutover"); at this scale they are multi-hour-to-day,
cost-significant events, and the §7(c) provisional ("parser wins ties") plus §7(d)
inferred-fact handling each risk *triggering* a major bump.

**Mitigation.**
1. **Cross-domain projection makes this worse (see S3-1) — entity retrieval must run
   per domain.** Cache entity-candidate and predicate-slice retrieval **per note**
   (one retrieval keyed by the note's mention set, reused across that note's
   candidates) instead of per candidate.
2. **Batch Stage 2 by shared retrieval context**: group candidates in a note that
   share a subject/predicate neighborhood so the injected slice is reused (prompt-
   prefix cache friendly). Quantify the batch factor in the eval harness.
3. **Make major-migration scope incremental and pinned-aware up front**: only
   re-extract notes whose facts touch the changed contract field (compute blast
   radius from the diff *before* re-running, not after). Avoid whole-corpus
   re-extraction as the default for a "major" bump; reserve it for genuine shape
   changes and otherwise prefer minor+deterministic-backfill.
4. Maintain a persistent ANN index for entities/predicates with incremental insert,
   not a rebuild-per-pass.

---

### S3-1 · Per-domain entity projections multiply rows **and** force entity resolution through a privileged cross-domain step on the hot path
**Severity: SEV-1** (this is also the §7(a) decision — performance is decisive here)

**Hot path.** The provisional §7(a) pick is F's **per-domain `entity_projection`**:
one row per `(canonical_entity, domain)`, threaded by RLS-scoped
`entity_identity(canonical_id, projection_id, domain_id)`. Facts reference a
**same-domain projection** (F §2.3, R4). Two hot paths suffer:

1. **Entity resolution at extraction** ("is this the same Dad?") must decide identity,
   but a session scoped to one domain can only see *that domain's* projections (F
   §2.3). Deciding "the health 'Dad' and the general 'Dad' are the same canonical
   entity" requires a **privileged dual-domain step** that briefly holds both
   domains' projections (F open-Q 1/2, carried forward in spec §3.3 "a new
   high-value asset"). This step is on the **write/ingest hot path** for every
   cross-domain mention, and it is the one step that *cannot* be RLS-narrowed, so it
   cannot benefit from the `domain_id`-leading index selectivity F relies on
   elsewhere.

2. **Read/render dereference.** Every edge render must join fact → same-domain
   projection (cheap), but any "show me everything about canonical entity C across
   what I'm allowed to see" must fan out across up to 4 projections via
   `entity_identity`, an extra join per entity per render.

**Cost reasoning.**
- **Row multiplication:** entities × domains projections. With 10^4 entities and an
  average of even 1.5 domains each, that is ~1.5×10^4 projection rows + the
  `entity_identity` join table — modest in absolute terms, but it **fragments entity
  resolution**: the candidate set for "Dad" is now split across domains, so retrieval
  recall (already the S2-1 killer) must query *per domain and reconcile*, multiplying
  the `3k` ANN queries of S2-1 by the domain count for cross-domain mentions.
- **The privileged resolver is an O(global-entity-set) operation with no firewall
  selectivity** — it is the one place the design deliberately removes the index
  partitioning that makes everything else tractable. Its latency grows with the total
  entity count and it is invoked on the ingest path.
- B's elegant **O(1) redirect-based split/merge** (B §2, §3.5) survives only
  *within* a domain (spec §3.3); **cross-domain identity merge becomes a gated
  `identity_merge` op** that must touch/relink projections in multiple domains — no
  longer O(1).

**Where it falls over.** The §7(a) "what would flip it" clause explicitly names "a
perf model showing projection proliferation breaks entity resolution recall." This
finding *is* that perf model: projection-per-domain doesn't break on row count, it
breaks on **resolution recall and the un-indexable privileged resolver on the ingest
hot path**. The security argument for projections is strong (the FK covert channel is
real), but the spec under-weights that it relocates entity resolution — the single
most latency- and recall-sensitive step (S2-1) — behind a deliberately
un-optimizable boundary.

**Mitigation / position on §7(a).**
- **Keep F's projection model** (the RLS invariant is binding and the FK leak is
  documented) **but** add the §7(a)(iii) hybrid's **attribute-free global
  `canonical` skeleton** *as an indexable resolution surface*: entity resolution runs
  against a global, attribute-free embedding/alias index keyed by `canonical_id`
  (carries no protectable value, so it is firewall-safe to index globally), and only
  *attribute rendering* goes through per-domain projections. This restores a single,
  globally-indexed resolution target (recall + latency) without re-introducing the
  cross-domain FK or a global attribute row.
- The privileged cross-domain resolver should operate over this attribute-free index,
  not over both domains' full projection rows — shrinking the "high-value asset" to an
  alias/embedding cluster with no protectable attributes.
- Budget the `identity_merge` op as O(projections-for-canonical), not O(assertions),
  by keeping merge as a redirect/`canonical_id` rebind, never an assertion rewrite.

---

## SEV-2 findings

### S2-2 · Append-only assertion growth → index bloat and unbounded history scans for bitemporal "as-of" queries
**Severity: SEV-2**

**Hot path.** `fact_assertion` is never updated in place; every edit, supersession,
retime, pin, confidence tweak, and reprocess writes a **new row** (B §1.3, §4 mapping
table). The live partial index stays small, but the **base table and all
non-partial indexes grow with total history** = live × avg-revisions. Bitemporal
"as-of" queries (G E7: "what did we believe in 2025?") gate on `tx_from <= ? AND
(tx_to IS NULL OR tx_to > ?)` and **cannot** use the `WHERE tx_to IS NULL` partial
index — they scan historical rows.

**Cost reasoning.** Avg-revisions is the multiplier. A heavily-corrected fact
(repeated retime/confidence/pin ops, plus reprocess passes that each write an assertion
even when the value is unchanged — B §4 "re-analysis runs as ops") can accumulate
dozens of versions. At 10^5 live facts × ~5 avg revisions = ~5×10^5 rows; reprocess-
heavy histories push past 10^6. As-of queries are O(history per slot); the GiST index
on `valid_range` helps valid-time overlap but **not** the `tx_*` belief-time gate.
Index bloat also slows the *write* path (every insert maintains N indexes including the
GiST).

**Mitigation.**
- Add a composite **`(owner_id, slot_key, tx_from DESC)`** btree (or BRIN on
  `tx_from` given append-only monotonicity) to bound as-of scans to a slot's history.
- **Do not write a no-op assertion on reprocess** when the re-derived fact is byte-
  identical to the live row (idempotent reprocess, D E3 already guarantees
  determinism) — gate the insert on an actual change. This is the single biggest lever
  on history growth.
- Consider **partitioning `fact_assertion` by `tx_to IS NULL`** (live vs archived) or
  time-partitioning archived rows so cold history doesn't bloat hot indexes.

### S2-3 · One-edge-per-value + per-cell review submission → O(members) op rows and lock contention for set-valued slots
**Severity: SEV-2** (this is the §7(b) decision — performance is decisive here)

**Hot path.** §7(b) provisional: storage is one-edge-per-value; the review card
presents N cells but **each cell lowers to a member-targeted op** on a
`value_identity`. A batch review of a wide set (e.g. an entity with 30 phone
numbers/employers/children edited at once, or a split of one fact into many) emits
**one op + one assertion insert per member**, all in **one transaction** sharing a
`batch_id` (B §4 atomicity).

**Cost reasoning.**
- **Op/assertion rows per edit = O(members touched)**, each with its own audit row +
  inverse. A batch undo re-opens/tombstones O(members) rows in one transaction.
- B risk 4 mandates a **per-slot advisory lock** to avoid live-uniqueness races. A
  batch touching M members takes M advisory locks in one transaction → lock-ordering
  and contention risk if concurrent ingest touches the same entity. For a single-owner
  system concurrency is low, but **ingest + an open review session on the same entity**
  is a realistic concurrent pair, and the long multi-statement review transaction
  (F §4.5 warns these are longer) holds locks longer.
- The fat-read (§4.3) for a wide set materializes N cells **each enriched with ranked
  entity candidates + enum domains + ui_capabilities** — that enrichment is itself
  O(members × candidate-retrieval), i.e. it re-triggers the S2-1 retrieval cost on the
  *read* path of review.

**Mitigation.**
- Per §7(b), keep one-edge-per-value at storage (correct), but **cap and paginate the
  cells in the review fat-read** so a pathologically wide slot doesn't build an
  unbounded enriched payload; lazy-load candidate enrichment per cell on demand.
- Keep batch transactions **bounded** (a max members-per-batch; split very large
  batches into multiple atomic sub-batches sharing a parent `batch_id` for undo).
- Acquire per-slot advisory locks in a **canonical order** (sorted by slot_key) to
  prevent deadlock between concurrent batch + ingest.

### S2-4 · rrule recurrence: lazy expansion is right, but "next occurrence" / cross-fact calendar queries have no index and risk unbounded expansion
**Severity: SEV-2**

**Hot path.** G C4/§2.3: recurrence stored as an rrule blob in `recurrence jsonb`;
instances **never materialized**; the realized set is computed for a bounded query
window `expand(rrule, dtstart, [a,b)) ∪ rdates − exdates` then per-instance overrides
applied. Hot queries: "what's on my calendar this month across all recurring facts"
and "next occurrence of fact F after now."

**Cost reasoning.**
- **No index on recurring instances** (there are no rows). "Show this month across all
  recurring facts" must (a) find candidate recurring facts whose
  `[valid_from, valid_to)` window overlaps the month, then (b) **expand each rrule in
  application code** and filter to the window. Step (a) is a `valid_range` GiST
  overlap (fine); step (b) is O(recurring-facts-in-window × instances-per-window) of
  CPU-side rrule expansion per query — G's own R3 flags this as an open Sev-2 ("is
  lazy expansion fast enough... index strategy?"). The spec carries the open question
  unresolved.
- **"Next occurrence after now"** is the worst shape: a naive expansion from `dtstart`
  forward must skip every past instance to reach the first future one — O(instances
  since dtstart) for a long-running daily rule (a 5-year daily habit = ~1800 skips per
  query). The `count_cap` (730, G §2.3) bounds a *single* expansion but turns a long
  habit into a multi-window paged expansion, and a `FREQ=DAILY` with no UNTIL that
  slips past the cap is a correctness/perf cliff (G R3).

**Mitigation.**
- For "next occurrence," expand from **`max(dtstart, now)` aligned to the rule**, not
  from `dtstart` — rrule libraries support `after(dt)`; never iterate from the origin.
- **Maintain a tiny per-fact `next_occurrence_at` cache column** (recomputed on write
  and lazily on read-past-it) so cross-fact calendar/agenda queries become an indexed
  `ORDER BY next_occurrence_at` over recurring facts instead of N expansions.
- Enforce G R3's "reject unbounded high-frequency rule without a cap" at the committer
  (validator backstop), not just as a soft `count_cap`.

### S2-5 · Per-fact deterministic backstop pass (span verify + typed re-derivation + embedding canonicalization) adds CPU + a vector query to every fact, including reprocess
**Severity: SEV-2**

**Hot path.** D §3: the validator runs **after every extraction, per fact**: B1 span
fuzzy-substring (Levenshtein over the cited span), B2 typed-value re-parse (unit
grammar / dateparser), B3 negation lexicon scan, **C2 predicate canonicalization
(embedding registry lookup + coined-slug ANN dedup)**, D1–D4 link/firewall checks
(entity existence under RLS scope). F1 calibration.

**Cost reasoning.** Most backstops are cheap per fact (regex/parse/lexicon over a
bounded span). The two that scale with corpus are **C2** (an embedding query +
ANN dedup against the *growing* predicate/coined-slug index — same index-growth
concern as S2-1) and **D1** (an RLS-scoped entity-existence lookup). Per fact this is
a handful of ms; the issue is the **multiplier on reprocess/migration**: the entire
backstop pass re-runs on every fact in a re-analysis (D E3 idempotence *requires*
re-running it to reproduce output), so a major migration pays the per-fact embedding
query 10^5 times (already counted in S2-1, but the backstop adds its own ANN query
distinct from the Stage-2 injection retrieval).

**Mitigation.** Cache predicate canonicalization results keyed by `(raw_predicate)` so
identical drift spellings don't re-query the registry every fact; reuse the Stage-2
entity-candidate retrieval result for D1 instead of a second lookup. Skip the full
backstop pass on reprocess when input note span + contract version are unchanged
(content-hash short-circuit), consistent with the S2-2 no-op-suppression lever.

---

## SEV-3 findings

### S3-2 · Contract envelope size inflates token cost per Stage-2 call
**Severity: SEV-3.** The `factclaim/1` envelope (§2.1) carries `temporal` (G object,
~10 fields × 2 endpoints), `process` provenance, `provenance` with denormalized
`quote`, slot, predicate. As an *input/output* schema for every per-candidate Stage-2
call, the verbose nested shape adds tokens to all `O(facts)` calls (S2-1). The
constrained-decode reliability concern (D open-Q 9, "schema size vs constrained-decode
reliability") has a cost twin: bigger schema = more output tokens per fact. *Mitigation:*
Stage 2 emits a **minimal** draft (the model-decided fields only); the committer
hydrates the full envelope server-side. Don't round-trip `process`/derived fields
through the model.

### S3-3 · Denormalized `quote` on every assertion + provenance duplication bloats row width
**Severity: SEV-3.** B §3.4 denormalizes the cited `quote` text onto provenance "for
audit stability," and the spec keeps `quote` in the envelope (§2.5). With append-only
history (S2-2), the quote string is **copied into every superseding assertion's
provenance** even when only `confidence` or `pinned` changed. Row/TOAST bloat ∝
history × quote length. *Mitigation:* store the quote once keyed by `(note_id, span)`
and reference it; supersessions that don't change provenance point at the same quote
row rather than re-copying.

### S3-4 · `valid_range` GENERATED + GiST maintained on every write of a write-heavy ingest
**Severity: SEV-3.** The STORED generated `valid_range` (§3.1) plus its GiST index is
recomputed and re-indexed on **every** assertion insert, including no-op reprocess
writes (until S2-2's suppression lands). GiST maintenance is costlier than btree.
*Mitigation:* covered by S2-2 (suppress no-op writes); ensure GiST is only consulted
for genuine overlap/Allen queries (E4), not for point current-value reads (which
should use the materialized current table, S1-1).

---

## Cross-cutting recommendation

The spec's animating principle — **derive everything from an append-only log, store as
little as possible** — is correct for *audit and reversibility* but is applied too
aggressively to the **read path**. At target scale the system is read-dominated
(renders, search, review loads, current-value lookups) over a 10^6-row history. Three
materializations move the design from "falls over" to "scales," all kept as
committer-maintained authoritative caches (rebuildable from the log, so audit/#7 are
untouched):

1. **`fact_current` / slot table** — current-value as a maintained point/range read
   (S1-1). *Promote B §5.4 from optional to baseline.*
2. **`next_occurrence_at` per recurring fact** — indexed agenda/next-occurrence
   (S2-4).
3. **Global attribute-free `canonical` resolution index** — restore a single indexed
   entity-resolution target without re-introducing the cross-domain FK (S3-1 / §7(a)).

Plus two write-path levers that bound history growth and migration cost:
**suppress no-op reprocess writes** (S2-2/S2-5) and **make major-migration scope
incremental from the contract diff** rather than whole-corpus re-extraction (S2-1).

§7 positions taken: **(a)** keep per-domain projections (security binding) **but** add
an attribute-free global resolution index — the bare projection model breaks entity-
resolution recall/latency on the ingest hot path; **(b)** one-edge-per-value at storage
is correct, but the per-cell fat-read enrichment and O(members) batch must be bounded/
paginated to scale.
