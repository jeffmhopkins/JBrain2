# Fix Options — ISSUE 2: No Mutual / Inverse Edge Logic

**Status:** design / options only — no source, prompt, migration, or test changes proposed for implementation here.
**Scope:** builds on a *correct directed edge* (Issue 1, the object-person mention + `object_entity_ref`, assumed fixed by another agent). This document does **not** redesign mention emission.

---

## 1. The problem, precisely

The pipeline materializes exactly the edges the model emits. Given a perfect directed
`Jeff.spouse → Celine`, nothing ever writes the reciprocal `Celine.spouse → Jeff`. Two
distinct gaps, conflated today:

- **Cardinality** — `FUNCTIONAL_PREDICATES` (`supersession.py:24`) governs *how many current
  values a predicate allows on one subject*. `spouse` is in that set, so Jeff gets one current
  spouse. This says nothing about direction.
- **Reciprocity** — symmetric relations need the *same* predicate on the other party
  (`spouse`, `sibling`, `co_founder`); asymmetric relations need a **named inverse** with a
  *different* predicate (`worksFor↔employs`, `parent_of↔child_of`, `manages↔reportsTo`,
  `tenant_of↔landlord_of`, `mentors↔mentee_of`, `hasTreated↔treatedBy`). The system has no
  concept of either.

`spouse`'s presence in `FUNCTIONAL_PREDICATES` creates a *false sense of completeness*: the
graph enforces "one spouse for Jeff" while Celine's side of the marriage is structurally
absent. For asymmetric relations the gap is sharper — the inverse predicate name (`employs`)
is never even constructed, so it cannot be derived by reflecting the edge alone.

### Why this is architecturally subtle (the three forces every option must answer)

1. **Supersession of the source must propagate.** When `Jeff.spouse` changes from Celine to
   Dana, the reciprocal `Celine.spouse → Jeff` must *also* close, and `Dana.spouse → Jeff`
   open. A stored reciprocal that does not move is worse than no reciprocal: it is a *stale
   lie* the wiki will publish. `decide()` (`supersession.py:235`) resolves one identity key in
   isolation; it has no awareness of a paired key on another entity.

2. **Note deletion purges derived artifacts and repairs chains.** `purge.py` hard-deletes
   every fact `WHERE note_id = :note` and repairs supersession chains (`_repair_chains`). A
   reciprocal edge is a *derived artifact* — what is its `note_id`? If it shares the source's
   note, purge deletes it for free; if it has none, purge cannot find it and it dangles.

3. **Cross-subject firewall.** `ANALYSIS.md` "Entities": `subject_id` on an entity makes
   fact→subject attribution a **security field**; cross-*subject* misattribution is a leak. An
   inverse edge *by definition* lands a fact on the **object** entity's stream. If Dr. Patel is
   a different security subject than Elena (Phase 7 intake-link), an inverse `Elena.treatedBy →
   Dr. Patel` writes a fact whose `subject_id`/`domain_code` belong to a *different subject*
   than the source. `_existing_facts` (`pipeline.py:602`) already filters by `domain_code`
   precisely because "without the explicit domain filter a health fact would supersede a
   same-key general fact and a review card would copy cross-domain text." Any inverse-edge
   design inherits this hazard and must answer it explicitly.

---

## 2. Ground truth from the codebase (what a design must fit)

- **Fact identity key** = `(subject_id, entity_id, predicate, qualifier)` (`facts` table,
  `0006_analysis_schema.py:151`; index `facts_identity_idx`). For *non-functional* predicates
  the pipeline additionally scopes candidate retrieval by `object_entity_id`
  (`pipeline.py:631`), so distinct edges (`me.owns→Civic` vs `me.owns→kayak`) are distinct
  facts. For *functional* predicates the object is deliberately left out of the key so a new
  employer supersedes the old.
- **`object_entity_id`** already exists on `facts` (`0006:167`) — the directed edge target.
  There is no column for "this fact is the inverse of fact X," no provenance pointer to a
  source fact, and no `derived`/`is_derived` flag.
- **Provenance is note-anchored.** Every fact has `note_id NOT NULL` (`ON DELETE CASCADE`),
  `chunk_id`, `extractor`, `prompt_version`, `confidence`. The whole deletion/purge model keys
  off `note_id`.
- **Supersession side effects** (`_apply_decision_side_effects`, `pipeline.py:841`) close old
  facts (`status=superseded`, `superseded_by`, SCD-2 `valid_to`), hold collisions, and file
  `review_items`. All driven by `Decision` from a *pure* `decide()` that sees only one key's
  facts.
- **`prompt.py`** already has the "decompose into two facts" precedent (`owns` →
  relationship + name attribute, lines 76–81, 123–130) and a canonical-predicate list
  (lines 49–57). `PROMPT_VERSION` (`note-extract-v4`) gates re-runs as planned migrations.
- **RLS:** every analysis table carries `domain_code` + `has_domain_scope` policy
  (`0006:227-245`); **CLAUDE.md rule 3 requires an RLS isolation test for every new table**
  (pattern in `test_analysis_rls.py::seed_health_graph`).
- **Predicate is free text, schema.org-guided, no controlled ontology** (`ANALYSIS.md`
  "Facts" **[decided]**). Nightly consolidation normalizes drift *toward* schema.org.

---

## 3. The options

### Option 1 — Extraction-time: the model emits both edges

**How it works.** Extend `SYSTEM_PROMPT` to instruct the model, after emitting a relationship
edge, to also emit the reciprocal: same predicate for symmetric relations, the named inverse
for asymmetric ones — directly modeled on the existing "`owns` decomposes into two facts"
pattern. The canonical-predicate block (`prompt.py:49`) gains an inverse-pair table
(`worksFor↔employs`, `parent_of↔child_of`, …) and a symmetric list. Bump `PROMPT_VERSION`.

**Schema / migration impact.** None. Both rows are ordinary facts on existing columns.

**Files / components touched.** `prompt.py` only (plus a corpus re-run on the new
`PROMPT_VERSION`). No pipeline, supersession, or migration change.

**Supersession behavior.** Both edges flow through `decide()` independently and *coincidentally
correctly* on the note that produced them. The failure is **later**: a *new* note "Jeff and
Celine divorced; Jeff married Dana" must re-emit *all four* edges (close Jeff↔Celine both ways,
open Jeff↔Dana both ways) for the graph to stay consistent. If any note touches only one side
("Jeff remarried"), the reciprocal on the other party never moves → **drift / desync**. The
two edges are independent rows with no link, so nothing reconciles them.

**Deletion / merge.** Free and correct: both rows carry the source note's `note_id`, so purge
deletes both; merge/split repoints both like any fact.

**RLS / cross-subject.** Each emitted fact carries its own `entity_ref`/`object_entity_ref` and
`domain`; the existing per-fact resolution and `domain_code` handling apply unchanged. The
reciprocal lands on the object entity's stream via the *normal* path, so it inherits the same
(unsolved-here) question of what subject/domain a fact about another subject should carry — but
introduces no *new* firewall mechanism. Misattribution risk = the model picking the wrong
`entity_ref`, same class of risk the pipeline already tolerates.

**Pros.** Simplest possible plumbing; reuses a proven prompt pattern; zero new tables → zero
RLS-test obligation; the model genuinely *knows* inverse predicates (schema.org vocabulary).

**Cons.** Doubles fact rows from the source of truth (against "extract less, not more" and the
`MAX_FACTS=12` cap — four-edge notes like Case 3/Case 10 eat the budget fast). **Desync on
single-sided supersession is the fatal flaw**: nothing keeps the two edges consistent once a
later note moves only one. Relies on per-note model discipline that the same Issue-2 evidence
shows the model currently lacks.

**Cost / risk.** Low build cost, **high correctness risk** (silent stale reciprocals).
**Effort: S.**

---

### Option 2 — Pipeline-derived inverse facts (materialized) *(detailed)*

**How it works.** The pipeline, after persisting a directed relationship edge, consults an
**inverse-predicate registry** (Option 4) and, when the predicate is symmetric or has a named
inverse, writes a **derived inverse fact** on the object entity, tagged as derived with
provenance pointing at the *source fact*, not directly at a note.

The derived edge for `Jeff.spouse → Celine` is `Celine.spouse → Jeff`; for `Marcus.worksFor →
Globex` it is `Globex.employs → Marcus`. It runs through `decide()` like any candidate (so it
participates in supersession/conflict on *its own* key — `Globex.employs` accumulates,
`Celine.spouse` is functional and supersedes), but is *marked* so the pipeline can propagate
and purge it as a dependent of its source.

**Schema / migration impact (new migration, e.g. 0011).**
- Add to `app.facts`: `derived_from_fact_id uuid REFERENCES app.facts(id)` (NULL = primary,
  human-authored-from-a-note fact; non-NULL = this row is the inverse of that fact). An index
  `facts_derived_from_idx ON (derived_from_fact_id)` for propagation/purge lookups.
- `note_id` stays NOT NULL and is **copied from the source fact** — the derived edge belongs to
  the same note that produced its source. This is the key decision that makes purge work for
  free: `DELETE ... WHERE note_id = :note` already deletes the derived edge; no new purge code
  for the common case. (`derived_from_fact_id` then needs `ON DELETE CASCADE` *or* the purge
  must delete derived rows first — see Deletion below.)
- **No new table** in this shape → **no new-table RLS obligation**, but the new *column* is a
  security-relevant field on a security-firewalled table, so the migration's RLS test must add
  a case proving a derived row obeys `has_domain_scope` and that a derived row's `domain_code`
  is set correctly (see Cross-subject below). If a designer instead chose a *separate*
  `derived_facts` table, that table would trigger the full new-table RLS isolation test per
  CLAUDE.md rule 3 — a reason to prefer the column.

**Files / components touched.** `pipeline.py` (`_upsert_fact` gains a post-insert "emit
inverse" step; supersession side-effects extended to propagate), `supersession.py`
(unchanged logic, but `decide` is now also called for derived candidates), a new
registry module (Option 4), the 0006-amending migration, `purge.py` (propagation on
chain repair — see below), and `test_supersession.py` / `test_analysis_rls.py` /
`test_extraction_pg.py` for coverage.

**Supersession propagation — the hard part.** Three events must keep the derived edge true:

1. **Source inserted** → emit derived candidate, run `decide()` on the object's key, persist
   with `derived_from_fact_id = source.id`, `note_id = source.note_id`.
2. **Source superseded** (a later note changes `Jeff.spouse`): `_apply_decision_side_effects`
   closes the old source fact. The propagation step must **also** close/supersede the old
   *derived* edge `Celine.spouse → Jeff`. Look up `WHERE derived_from_fact_id = old_source.id`
   and apply the same close (status `superseded`, SCD-2 `valid_to`, `superseded_by` →
   *the new source's derived edge*). Because the new note also produces a new source
   (`Jeff.spouse → Dana`), its derived edge (`Dana.spouse → Jeff`) is minted in the same run,
   giving a clean inverse chain that mirrors the source chain.
3. **Source refreshed / interval-closed** (`refresh_id` / `close_id` paths): propagate the same
   in-place update to the derived row (statement/value re-render, `valid_to` copy). The derived
   row carries its own statement ("Celine's spouse is Jeff"), regenerated from the source.

The invariant: **a derived edge's lifecycle is a shadow of its source's lifecycle.** It never
makes an independent supersession decision *against the source*; it only mirrors. But on the
object's *own* key it must still respect existing facts (e.g. if Celine already has a
human-authored `spouse` fact from another note, the derived candidate must not silently
supersede it — it should route to `fact_conflict`/`pending_review` like any collision, so a
human adjudicates a contradiction between a primary and a derived claim). **Design rule:** a
derived candidate may supersede another *derived* row freely (shadow of its source) but must
**defer to / conflict with** a *primary* row on the same key — never auto-overwrite human-or-
note-sourced knowledge with a reflection.

**Deletion behavior.** Because `note_id` is copied, the source-note delete deletes both the
source and its derived edges in the same `DELETE WHERE note_id` (`purge.py:92`). The subtlety:
purge's `_repair_chains` reattaches survivors whose `superseded_by` is doomed. A derived
*chain* must repair the same way — and since derived rows share their source's `note_id`,
derived links inside one note die together (consistent). The genuinely new case: a **survivor
derived edge** whose source lives in a *surviving* note but whose *chain* points through a
doomed derived link — handled by the same `chain_repair_target` walk, since derived rows are
ordinary `facts` rows visible to that query. **One required addition:** if a *primary* source
fact is deleted but its derived edge somehow has a different `note_id` (it should not, by the
copy rule), `derived_from_fact_id` with `ON DELETE CASCADE` is the backstop. Recommend
**both**: copy `note_id` (so the common path is free) *and* `ON DELETE CASCADE` on
`derived_from_fact_id` (so a derived edge can never outlive its source).

**Merge / split.** Merge repoints `entity_id`/`object_entity_id` on all facts including derived
ones (ordinary rows). Split (two-people-merged, `ANALYSIS.md`) re-resolves mentions; derived
edges re-point with their source's object. No special casing beyond treating derived rows as
facts — *except* the review-reopen effects-unwind (`ANALYSIS.md` "Resolutions record their
graph effects"): if a human pins/retracts a *source* fact, the effect must cascade to its
derived edge, and reopen must reverse both. This is the one place derived edges add surface
area to the existing effects machinery.

**Review inbox interaction.** A derived edge that *conflicts with a primary fact on the object*
files a normal `fact_conflict`/`attribute_collision` card — but the card's copy and provenance
must make clear one side is *derived* (so the human isn't asked to adjudicate the system's own
reflection against itself). Derived-vs-derived never files a card (pure shadow). Contested
derived edges, like any unreviewed supersession, stay out of wiki builds.

**RLS / cross-subject safety — the firewall answer.** This is where Option 2 must be most
careful. The derived edge lands on the **object** entity's stream:
- **`domain_code`:** the derived edge inherits the **source fact's** `domain_code`, *not* the
  object entity's domain. This matches `_existing_facts`'s existing discipline (candidate
  retrieval is domain-scoped so a health fact never supersedes a general one) and the
  `ratchet_domain` rule that "facts always carry their own domains." A health-domain
  `hasTreated` source therefore produces a health-domain `treatedBy` derived edge — both behind
  the health RLS policy. The RLS test must prove a `health` derived edge is invisible to a
  `general`-only scope.
- **`subject_id` (the leak vector):** the derived edge's `subject_id` is the **object entity's**
  `subject_id` (resolved, same as a normal fact whose `entity_ref` is the object). If the object
  entity is *not* a security subject (`subject_id IS NULL`), no cross-subject question arises.
  **If the object entity IS a distinct security subject** (Phase 7: Dr. Patel and Elena are
  different subjects), writing `Elena.treatedBy → Dr. Patel` as a derived fact attributes a
  fact to Elena's subject that originated in a note authored under a *different* subject. That
  is exactly the cross-subject attribution the firewall guards. **Design rule (firewall-safe):**
  *do not auto-materialize a derived edge across a subject boundary.* When the object entity has
  a `subject_id` that differs from the source fact's `subject_id`, route the proposed inverse to
  the **review inbox** (a new lightweight `inverse_proposal` reuse of `review_items`, or
  reuse `fact_conflict`/a `domain_promotion`-style proposal) instead of writing it. Same-subject
  and null-subject inverses materialize automatically; cross-subject inverses are *proposed,
  never written*. This is the single most important rule in this document for avoiding a leak.

**Pros.** Single source of truth (the directed edge) with the reciprocal kept *consistent by
construction* — supersession propagation is explicit, not hoped-for. Graph queries and the wiki
read real rows (no reader needs the inverse map). Deletion is nearly free (note_id copy).
Functional-vs-symmetric distinction handled correctly per-side because each edge still runs
`decide()`. The firewall has a clean, explicit answer.

**Cons.** The meatiest implementation: propagation logic in three supersession paths
(insert/supersede/refresh/close), the effects-unwind cascade, and the cross-subject gate. Extra
rows (same row-count cost as Option 1, but *managed*). New column on the most security-sensitive
table → careful RLS test additions. Real complexity lives in "derived defers to primary" and
the reopen cascade.

**Cost / risk.** Moderate build cost, **low correctness risk** (consistency is enforced, not
emergent). **Effort: L.**

---

### Option 3 — Query-time / virtual inverse (no stored row)

**How it works.** Store only the directed edge. Synthesize the reverse at read time: graph
queries and the wiki traverse `object_entity_id` backwards and apply a **predicate-inverse map**
to relabel the predicate (`worksFor`→`employs`) when presenting the object's relationships. No
inverse rows ever exist.

**Schema / migration impact.** None for storage. Possibly a *covering index* to make the
backward traversal efficient: `facts (object_entity_id, predicate)` — without it, "who works at
Globex?" is a full-scan on `object_entity_id` (Case 9's noted weakness). One small additive
migration for the index; **no new table → no new RLS-test obligation**, though the index should
be validated under RLS.

**Files / components touched.** Every *reader*: the wiki builder, entity/graph query layer
(`EntityScreen` API, search), and any role-reference resolver. The inverse map lives in shared
config (Option 4). No pipeline/supersession/purge change at all.

**Supersession / conflict semantics.** **No desync — there is nothing to desync.** The reverse
view is a pure function of the directed edge, so when `Jeff.spouse` supersedes, Celine's
synthesized reverse changes automatically on the next read. *But:* the reverse view's
supersession/conflict semantics must be *defined for the reader*. If Celine has a *primary*
`spouse` fact (from her own note) **and** a synthesized reverse from Jeff's note, which wins on
display? The reader must merge a real chain with a virtual one and decide precedence — logic
that Option 2 centralizes in `decide()` but Option 3 *scatters into every reader*. There is no
place to file a `fact_conflict` for a contradiction that exists only in the virtual view.

**Deletion / merge.** Trivially correct: deleting Jeff's note removes the directed edge and the
virtual reverse vanishes with it. Merge/split touch only the stored directed edges.

**RLS / cross-subject.** **This is the option's hidden trap.** A reverse view of
`Jeff(health).hasTreated → Elena` synthesizes `Elena.treatedBy → Jeff` *at read time*. The
source row's `domain_code`/`subject_id` are the *source's*. A reader operating under *Elena's*
subject scope (or a non-health scope) must not see a synthesized edge derived from a row its RLS
*hides*. Because the synthesized edge is computed in application code from a row the reader can
read, RLS still gates the *source row* — but the moment a query *joins across* entities to build
the reverse view, it can surface the existence of a hidden-subject relationship. Every reader
must independently re-implement the cross-subject suppression that Option 2 enforces once. **The
firewall guarantee degrades from "enforced in one place" to "re-proven in every reader" — a
poor fit for a system whose CLAUDE.md rule 3 pushes enforcement into Postgres.**

**Pros.** Zero extra rows, zero desync, zero deletion cascade, zero supersession propagation.
Conceptually clean: one edge, one truth.

**Cons.** Every reader must know the inverse map and re-implement reverse-view precedence *and*
cross-subject suppression. No native slot for a primary-vs-reverse conflict in the review inbox.
Backward traversal needs an index to be efficient. **Firewall enforcement scatters** — the
opposite of the codebase's "enforce in Postgres / one place" doctrine.

**Cost / risk.** Low storage cost, **high architectural risk** (firewall + precedence logic
duplicated across readers, easy to get subtly wrong in one of them). **Effort: M** (small core,
but spread across many reader call-sites and the wiki).

---

### Option 4 — Registry / ontology data model (cross-cutting, feeds 1–3)

This is not an alternative to 1–3 but the **shared substrate** they all need: *where do the
symmetric set and inverse pairs live, and how do they stay matchable across prompt/model
versions?*

**Sub-option 4a — Code constant (frozenset/dict), like `FUNCTIONAL_PREDICATES`.** Define
`SYMMETRIC_PREDICATES = frozenset({...})` and `INVERSE_PAIRS = {"worksfor": "employs",
"employs": "worksfor", "parent_of": "child_of", ...}` next to `FUNCTIONAL_PREDICATES` in
`supersession.py`, normalized lowercase with schema.org + snake_case twins (the same dual-
spelling trick `FUNCTIONAL_PREDICATES` already uses on line 24). **Pros:** zero migration, unit-
testable, version-controlled, reviewable, ships in the same PR as the logic (CLAUDE.md rule 5).
**Cons:** changing the ontology is a code deploy. **Fits the codebase's existing pattern
exactly.**

**Sub-option 4b — Config (`JBRAIN_*` env JSON), like `JBRAIN_LLM_PRICES`.** The price table
precedent (`ANALYSIS.md` "Cost estimates") shows the system already loads ontology-ish data from
config JSON. **Pros:** tweak without deploy. **Cons:** untested data path, drift risk, no review
gate — wrong for a *security-relevant* mapping that decides where facts land.

**Sub-option 4c — Database table (`app.predicate_inverses`).** A table of
`(predicate, inverse_predicate, symmetric bool)`. **Pros:** queryable, editable at runtime,
joinable. **Cons:** a **new table → mandatory RLS isolation test (CLAUDE.md rule 3)** for what
is owner-only reference data (would need an owner-only policy like `llm_usage`); heavyweight for
a small static map; and predicates are free text with no controlled ontology
(`ANALYSIS.md` **[decided]**), so a rigid table fights the schema.org-attractor design.

**Graceful degradation (all sub-options).** An *unknown* predicate (not symmetric, no named
inverse in the registry) **emits no inverse** — the directed edge stands alone, exactly as
today. This is the safe default: the registry is an *allowlist of relations we know how to
reciprocate*, never a requirement. Nightly consolidation (which already normalizes predicate
drift toward schema.org) is the natural place to surface "predicate X looks like it wants an
inverse" as a future suggestion, but unknowns must never block or guess.

**Recommendation for 4:** **4a (code constant)** — it matches `FUNCTIONAL_PREDICATES` /
`SCHEDULE_PREDICATES` precisely, is testable and reviewable, ships with its logic, and avoids
the new-table RLS obligation. Reconsider 4c only if the relation ontology grows large enough to
warrant runtime editing, at which point it earns its RLS test.

---

## 4. Comparison at a glance

| Dimension | 1 Extraction-time | 2 Pipeline-derived | 3 Query-time virtual |
|---|---|---|---|
| Extra stored rows | Yes (doubled, unmanaged) | Yes (doubled, managed) | None |
| Desync on single-sided supersession | **Yes (fatal)** | No (propagated) | No (computed) |
| Supersession propagation code | None | Significant (3 paths + effects-unwind) | None |
| Deletion cascade | Free | Near-free (note_id copy + CASCADE) | Free |
| Reader complexity | None | None | **High (every reader)** |
| Review-inbox conflict slot | Reuses existing | Clean (derived-vs-primary) | **None (virtual conflicts)** |
| Cross-subject firewall | Same risk as today | **Enforced once (gate)** | **Scattered across readers** |
| New table → RLS test | No | No (new *column*) | No |
| Effort | S | L | M |
| Correctness risk | High | **Low** | High |

---

## 5. Recommendation

**Primary architecture: Option 2 (pipeline-derived, materialized) with the Option 4a code-
constant registry, and a strict cross-subject gate.**

Rationale:
- It keeps a **single source of truth** (the directed edge) while making the reciprocal
  **consistent by construction** — the only option that *enforces* consistency rather than
  hoping the model (Option 1) or every reader (Option 3) gets it right each time.
- It localizes the **firewall guarantee in one place**: the cross-subject gate that *proposes
  rather than writes* an inverse across a subject boundary. This matches the codebase's
  "enforce in Postgres / one place" doctrine and `_existing_facts`'s existing domain-scoping
  discipline. Option 3's scattering of that guarantee across every reader is the strongest
  reason to reject it for a system whose non-negotiables put domain firewalls in the database.
- Deletion is **nearly free** because the derived edge copies the source's `note_id`, so
  `purge.py`'s existing `DELETE WHERE note_id` and chain-repair already cover it; the
  `ON DELETE CASCADE` on `derived_from_fact_id` is a belt-and-suspenders backstop.
- It avoids a new table (the new *column* on `facts` carries the marker), so it incurs **no
  new-table RLS isolation obligation** — only *additional cases* in the existing
  `test_analysis_rls` suite proving a derived row obeys `has_domain_scope` and that a
  cross-subject inverse is *proposed, not written*.

**Interaction with the Issue-1 prompt fix.** Issue 1 makes the model emit the object person as
a real mention with a resolvable `object_entity_ref` — that is the *precondition* Option 2 needs
(no resolved object entity → no inverse possible, `pipeline.py:680` already drops object-less
relationship facts). The division of labor is clean:
- **Issue 1 (prompt):** "emit the directed edge correctly, with the object as a mention."
- **Issue 2 (pipeline, this doc):** "given a correct directed edge, materialize and maintain
  the reciprocal." The model is **not** asked to emit the inverse (that is Option 1, which we
  reject for desync) — the pipeline owns reciprocity, the same way the pipeline (not the model)
  owns supersession, entity resolution, and chain repair. This keeps the prompt's job small and
  the consistency-critical logic in tested Python.

**The biggest risk to call out explicitly: cross-subject leakage.** An inverse edge *by
definition* writes a fact onto the **object** entity's stream. If that object is a distinct
security subject (Phase 7 intake-link: Dr. Patel ≠ Elena ≠ the owner), auto-materializing the
inverse would attribute a fact to a subject other than the one whose note produced it — a
firewall breach. **The recommended design forbids this:** the inverse materializes
automatically **only when the object entity's `subject_id` is NULL or equals the source fact's
`subject_id`**; when they differ, the inverse is routed to the **review inbox as a proposal and
never written**. The derived edge always inherits the **source fact's `domain_code`** (never the
object entity's), so a health-domain source can never silently produce a general-domain
reciprocal, and RLS keeps the derived edge behind the same policy as its source.

---

## 6. Phased path

**Phase A — Registry + symmetric-only, same-subject (S).** Add `SYMMETRIC_PREDICATES` and
`INVERSE_PAIRS` constants (4a) beside `FUNCTIONAL_PREDICATES`, with schema.org + snake_case
spellings. Implement inverse materialization in `_upsert_fact` **only for the same-subject /
null-subject case** and **only on initial insert** (no propagation yet) for *symmetric*
predicates (`spouse`, `sibling`, `co_founder`) where the inverse predicate equals the source —
the lowest-risk slice (Cases 1, 2, 3, 10). Add the `derived_from_fact_id` column migration with
its RLS-test additions. Derived-vs-primary collisions route to the existing `fact_conflict`.

**Phase B — Supersession + close/refresh propagation (M).** Extend
`_apply_decision_side_effects` and the `refresh_id`/`close_id` paths to propagate to derived
rows via `derived_from_fact_id`. Add the effects-unwind cascade so pin/retract/reopen on a
source carries to its derived edge. This is what closes the *desync* gap and makes the reciprocal
trustworthy enough for the wiki.

**Phase C — Asymmetric named inverses (M).** Activate `INVERSE_PAIRS` for the asymmetric
relations (`worksFor↔employs`, `parent_of↔child_of`, `manages↔reportsTo`, `tenant_of↔landlord_of`,
`mentors↔mentee_of`, `hasTreated↔treatedBy`), handling the differing inverse predicate name and
the functional-asymmetry (one side functional, e.g. `worksFor`, the inverse `employs`
accumulating). Cases 4–9.

**Phase D — Cross-subject proposal path (M, Phase-7-aligned).** Implement the
`subject_id`-mismatch gate that *proposes* rather than writes a cross-subject inverse, with a
dedicated review-item presentation. This can ship as "always propose (never write) when the
object is subject-linked" from Phase A as a conservative default, then relax to "write when same
subject" — i.e. the gate is the *first* safety rule, even though the cross-subject *proposal UI*
is Phase-7-aligned. Until then, subject-linked objects simply get no auto-inverse (safe
degradation, identical to today's behavior).

Each phase ships its tests in the same PR (CLAUDE.md rule 5): supersession-propagation unit
tests in `test_supersession.py`, RLS/cross-subject cases in `test_analysis_rls.py`, and
end-to-end derived-edge cases in `test_extraction_pg.py` / the harness scenarios.

---

## 5-line summary

Compared four architectures for materializing the missing reciprocal/inverse edge: (1)
**extraction-time** — model emits both edges (simplest, but doubles rows and silently desyncs
when a later note supersedes only one side); (2) **pipeline-derived materialized** — the
pipeline writes a derived inverse fact tagged via a new `derived_from_fact_id` column, with
explicit supersession propagation, near-free note-deletion purge (copy the source's `note_id`),
and a code-constant inverse registry; (3) **query-time virtual** — no stored row, synthesize the
reverse at read time (no desync, but firewall enforcement and conflict precedence scatter into
every reader); plus (4) a **registry** sub-analysis recommending a code constant beside
`FUNCTIONAL_PREDICATES`. **Recommendation: Option 2 + registry 4a**, with the inverse owned by
the pipeline (not the prompt) so it composes cleanly with the Issue-1 mention fix. **Biggest
risk: cross-subject leakage** — an inverse lands a fact on the object's stream, so the design
auto-materializes only when the object's `subject_id` is NULL or matches the source, routes any
cross-subject inverse to the review inbox as a proposal (never written), and inherits the source
fact's `domain_code` so RLS keeps every derived edge behind its source's domain policy.
