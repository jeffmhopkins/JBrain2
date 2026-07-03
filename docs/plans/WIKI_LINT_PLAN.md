# JBrain2 — `wiki_lint` Build Plan (corpus-wide wiki health pass)

> **Status:** In progress · **Last verified:** 2026-07-03 · **Waves:** W0✅ A✅ B◻️
>
> **Wave A — as-built (shipped, 2026-07-03).** `wiki/lint.py` (`WIKI_LINT_SPEC` +
> `wiki_lint_handler` + `WikiLinter`), wired into the worker/API registries and the
> three lockstep tests; seed migration **0115** (`nightly_wiki_lint`, disabled,
> manual-fireable); integration suite `tests/integration/test_wiki_lint_pg.py`
> (8 tests incl. the 100% security-path firewall test and the index re-dirty
> convergence/second-run-stability tests). **Two deviations from the design below,
> reconciled here per DOC_LIFECYCLE:**
> 1. **Report sink is structlog + the `runs` run-log, NOT the Talk board.** The
>    design named a "Talk build-log summary" as output #4, but `wiki_talk_topics.article_id`
>    is `NOT NULL` (FK to a single article) — a corpus-wide summary has no article to
>    anchor to, and making that column nullable is a non-additive schema change. So the
>    `LintReport` (weak-signal counts) is emitted via `log.info("wiki_lint_report", …)`
>    and captured in the fire's `runs` step log (the shipped audit surface). Every
>    "Talk build-log" reference below should be read as "the `runs` lint report"; a
>    per-article-anchored Talk post is a possible later refinement. The no-leak property
>    is unchanged (counts + no titles/bodies/domain-names).
> 2. **Reciprocal-asymmetry weak-signal count is deferred** from the v1 slice (checks
>    3/4a/4b/5a/5b + index-integrity shipped). It is never a card and minting the
>    reciprocal is graph mutation the linter must not do; add it as a `LintReport` field
>    when a consumer surfaces it.
>
> Owner-approved review direction (the "third leg" — a periodic corpus-wide wiki
> HEALTH audit alongside ingest `wiki_refresh`/`wiki_rebuild` and query
> `search`/`agent`), grounded by five scoped codebase researchers (engine &
> action-registration, review-inbox/correction path, wiki data model, fact-graph
> contradiction feasibility, RLS/session-scope) plus the process/CI/docs surface.
> Waves per `docs/reference/PROCESS.md`: one PR per wave, per-task + per-wave
> adversarial review, CI green before merge. This plan is **additive on shipped
> Phase-6/Phase-5 primitives** — review inbox, the `wiki_built` dirty bit and its
> three shipped triggers, `wiki_links`, `entity_mentions`, `runs`, Talk, the
> in-code `ActionSpec` pattern — and adds **no new table**. Open owner decisions
> are collected in §9 with recommended defaults; PROCESS.md names "plan open
> decisions" as owner-escalation items, so §9 is presented for **explicit owner
> ratification in one pass at plan sign-off, before the Wave A branch is cut**
> (Wave W0). The plan file is **`docs/plans/WIKI_LINT_PLAN.md`**.
>
> **DOC_LIFECYCLE transition 2 (this Scheduled filing).** The Scheduled filing
> adds BOTH the `docs/plans/README.md` row AND a `docs/ROADMAP.md` entry in the
> **same act as sign-off**. The ROADMAP entry **is filed now** (verified added to
> `docs/ROADMAP.md`) under a **dedicated, clearly-labelled sub-section
> `### Wiki health sweep (separate plan) — Scheduled`** that names **this plan's own
> path** (`docs/plans/WIKI_LINT_PLAN.md`). It is deliberately **NOT** parked inside
> the `## Phase 6 — Wiki — In progress` prose, because that header is bound to a
> **different** plan (`docs/plans/PHASE6_WIKI_PLAN.md`); conflating the two would
> imply `wiki_lint` is part of the PHASE6_WIKI_PLAN waves. Wave A (T-A4) only
> **flips** that existing entry to *In progress* (transition 3); it does not create
> it. Wave B (T-B4) flips it to *Shipped* and moves it under
> `## Phase 6 follow-ons — Shipped` at archive.
>
> **Wave W0** is a no-PR owner-ratification gate. Per DOC_LIFECYCLE line 107 a
> header glyph flip must land in a **merged edit (a PR)**, and a branch-cut is not
> one — so the **`W0` glyph flip rides in the Wave A PR** (the first PR after
> ratification), alongside the Wave A `A◻️→✅` flip, rather than "at branch-cut."
> The header therefore reaches all-✅ inside merged PRs before the R4 archive check.

## Thesis

Per-build verification — the grounding gate (`wiki/rewriter.py:157` `_ground`) —
is **single-entity, single-build**: it only ever sees one entity's own sourced
claims against that build's own drafted clauses (`rewriter.py:160`, sourced from
`builder.py:531 WHERE f.entity_id = :e`). It never compares two articles, two
entities, or the corpus against itself. Two articles can each pass their own
grounding gate and still contradict each other.

**But most "self-heal that silently didn't happen" framings are wrong**, because
the dirty bit already moves on the mutations that matter. Three shipped triggers
(migration `0046`) drive it: `wiki_entity_self_dirty` (identity-column changes),
`wiki_dirty_entity_from_fact` (AFTER INSERT/UPDATE/DELETE on `app.facts` — dirties
`entity_id` **and** `object_entity_id`, `AND wiki_built` guard, SECURITY DEFINER),
and `wiki_dirty_entity_from_mention` (AFTER INSERT/UPDATE/DELETE on
`app.entity_mentions`). So a fact retraction, a fact edit, a mention rewrite, and
an entity merge **already re-dirty** the citing/mentioned entities; the next
`wiki_refresh` re-sources and self-heals. **Verified consequence:** a note/chunk
purge is note-level and runs `delete(Fact).where(Fact.note_id==note_id)`
(`analysis/purge.py:94`), which fires `wiki_dirty_entity_from_fact` per deleted
fact, and every builder citation is fact-backed (`_source` selects `f.id AS
fact_id`, `builder.py:523/557`, never NULL). So a purge that CASCADE-deletes a
`wiki_citations` row **also re-dirties the citing entity** — the "dangling `[n]`"
state is *coupled* to a re-dirty and is self-healing (see check 6, **DROPPED**).

**The re-dirty channel is far narrower than first framed — because re-dirty only
CONVERGES against the PRODUCTION build path where the missing artifact is
re-derived WITHOUT depending on the LLM re-drafting a specific clause AND without
depending on the section that carries the artifact reappearing in the next plan.**
Verified: link and index rows are deleted/re-written **per section, only for
sections present in the NEW `plan.sections`** — `_write_section` DELETEs
`wiki_links` for `from_section_id` (`builder.py:788`) and upserts `wiki_index`
(`builder.py:809`), and it is called **only** inside the `_build_entity` loop over
the new plan's sections (`builder.py:455-456`). **There is NO section
reconciliation on rebuild** — neither `_ensure_article` (`builder.py:642-693`) nor
`prune` (`builder.py:389-415`) ever deletes or archives a `wiki_section`. So a
section that **drops out** of the next plan (its clause dropped by grounding, its
heading restructured by the LLM) keeps its stale body, stale
`current_revision_id`, **and its stale `wiki_links`/`wiki_index` rows** — the
DELETE/upsert never fires for it. This is the single fact that governs which
findings may be re-dirtied.

The genuine standing drift the nightly build **cannot** catch splits into two
buckets — the LLM-shaped case (Wave B) and a set of deterministic weak signals
that must be **counted, not re-dirtied**, because re-dirtying them either loops or
depends on a section reappearing:

- **Cross-entity semantic disagreement** — two entities' articles asserting
  contradictory prose. No single fact status, no trigger, no deterministic column
  detects it; it is intrinsically LLM-shaped (check 1, Wave B).
- **A red-link whose target became notable** (check 5a) — a `wiki_links` row with
  `to_article_id IS NULL` whose `to_entity_id` **later** got an active article.
  This was previously framed as "the one unconditionally-convergent re-dirty
  class." **That is now corrected: it is NOT unconditionally convergent.** A
  rebuild replaces the source's `wiki_links` rows **only for sections that reappear
  in the new plan**. If production grounding drops the sole clause carrying the
  A→B relationship, or the LLM restructures the heading, the red-link's section
  vanishes from the plan, `_write_section` never runs for it, the per-section
  DELETE never fires, and the stale red-link row **survives the rebuild** →
  check 5a re-fires → re-dirty → rebuild → section still dropped → **unbounded
  `cost_class='expensive'` rebuild loop.** The `StubRewriter` (`builder.py:169-175`)
  emits exactly one section per domain containing every `object_entity_id` claim,
  so the red-link's section **always** exists under the stub and a stub-driven test
  would show false convergence — the exact trap this plan rejects for the other
  checks. **Therefore check 5a is a Talk-count-only weak signal, never
  re-dirtied** (see §2, check 5).

**Explicitly NON-convergent classes (weak signals → Talk count, never
re-dirtied): checks 3, 4a, 4b, 5a, 5b.** Link emission in production runs through
the LLM rewriter, which appends a `PlannedLink` **only** inside `_assemble`'s loop
over the **grounded `kept` clauses** (`rewriter.py:221-256`, gated by `_ground` at
`:157`) — NOT the deterministic `StubRewriter`. If the LLM never drafted (or
grounding dropped) the clause, or the carrying section is restructured away, a
rebuild will **again** not emit/clear the row → the finding recurs → unbounded
expensive rebuild. All such classes are surfaced as **Talk-log weak-signal
counts**, never re-dirtied.

**What CAN be re-dirtied.** After demoting 5a, the **only** re-dirty leg is the
**optional index-integrity signals** (§2, owner decision §9-6a), and even those
are convergent **only for sections that reappear in the next plan** — the dominant
case for a stable, still-notable entity. They are explicitly **scoped to
reappearing sections**, with the orphaned-section population excluded (§2). **If
the owner declines §9-6a, Wave A ships with NO re-dirty leg at all** — it is a pure
Talk-log + `runs` audit. This is stated honestly rather than manufacturing a
convergent class that production does not deliver.

`wiki_lint` is a **fifth in-code `ActionSpec`** on the shipped hygiene-sweep
pattern (`analysis/hygiene.py` — `entity_hygiene`/`reembed_stale`/
`tag_consolidate`, migration 0066; and the four wiki actions in
`wiki/actions.py`). It is **read-only against the wiki** — it never edits an
article, so the machine-written doctrine (CLAUDE.md non-negotiable #7) is
preserved verbatim. Its only outputs are the four the design names, all built
from primitives already shipped:

1. **Review-inbox items** for human judgment (`app.review_items`, existing
   table, new `kind`s via the established CHECK-extension migration pattern).
2. **Re-dirtying entities** (`UPDATE app.entities SET wiki_built=false`) so the
   next nightly `wiki_refresh` self-heals the drift — **only for the optional
   index-integrity signals, scoped to reappearing sections** (§9-6a). No
   deterministic check re-dirties by default.
3. **A `runs` row** — automatic for any scheduled/Ops-fired pipeline
   (`PipelineRunLog.record` at `scheduler.py:307`), no new code.
4. **A Talk Build-log summary** — domain-neutral counts, written to the
   owner-only Talk board like the builder's existing build-log summaries. The
   sink for **all non-convergent weak signals** (red-link-became-notable,
   coverage gaps, fact-backed missing-xref/inbound, bare co-mentions, reciprocal
   asymmetry, cross-firewall exclusions) that must be surfaced but must NOT drive
   a re-dirty.

## 0. What this plan does NOT do

- **Never edits an article.** No `wiki_lint` output writes `wiki_revisions`,
  `wiki_sections`, or article prose. Every "fix" resolves to (a) a correction
  note, (b) a re-dirty / `request_rebuild`, or (c) a staged entity merge/split
  Proposal — the shapes the shipped resolution/agent actions already take.
  `mutating=True` on the spec means DB blast-radius (it writes `review_items` and
  flips the dirty bit), **not** article mutation; the spec description states the
  read-only-article guarantee explicitly so a reviewer can't mis-flag it.
- **No new table.** Findings ride `app.review_items` (domain-scoped, RLS-shipped)
  + the `entities.wiki_built` bit + `runs` + Talk. This is the deliberate design
  choice that avoids a per-table RLS isolation obligation (CLAUDE.md #3); see §5.
- **No `app.actions` row.** Like every other sweep, `wiki_lint` is registered
  **in-code only** (composed into the worker + API registries at boot), so the
  0035 seed-lockstep (`test_actions_rls.py`) stays green untouched. Migration
  0035 and `ACTION_SPECS` are **not** touched.
- **Does not re-do `wiki_prune` or the citation cascades.** `wiki_prune` archives
  **only** articles whose anchor `entity_ref` is **GONE (purged)** — verified
  `builder.py:389-415` (`WHERE status='active' AND entity_ref IS NOT NULL AND NOT
  EXISTS (SELECT 1 FROM app.entities e WHERE e.id=a.entity_ref)`). It does **not**
  touch zero-inbound-link articles. `wiki_lint` audits (read-only) the drift these
  **miss** — the red-link-became-notable and missing-link / coverage weak signals
  it counts to Talk — and defers the deletion classes to prune (see §2, check 5).
- **Does not audit purge-coupled dangling `[n]` markers** — that state is
  self-healing (the fact trigger re-dirties the citing entity), so **check 6 is
  DROPPED** (see §2). This drop is **contingent on the current fact-backed-citation
  invariant** (every `wiki_citation` carries a non-null `fact_id`); Wave A defends
  the invariant with a guard test (T-A3), and if a chunk-only citation path is ever
  introduced the check is re-scoped (see check 6).
- **No corpus rebuild of its own.** It re-dirties (only the optional
  index-integrity signals); the nightly `wiki_refresh` (which builds `WHERE NOT
  wiki_built`, `builder.py:311`) does the healing.
- **Does not audit builder-deliberate no-article states as drift.** A notable
  entity that the builder marked built with **zero sections** (empty `plan.sections`
  → `_mark_built`, no article — `builder.py:444-453`, deliberate for entities
  "notable only via mentions" or whose facts were all dropped by the
  same-domain-chunk skip) is **permanent, intended** behaviour, not drift. Check 3
  is scoped around it (see §2) so the sweep never re-dirties an entity that
  provably re-derives zero sections — the exact non-converging expensive-rebuild
  loop this plan must not create.
- **Notes remain the sole sources of truth; the wiki stays machine-written.**

## 1. Shared posture (the fifth sweep)

- **In-code `ActionSpec`, standalone factory shape.** A module-level
  `WIKI_LINT_SPEC` (`name='wiki_lint'`, `version=1`, `handler='wiki_lint'`,
  `domain_optional=True`, `mutating=True`, `dedup_key_expr=None`,
  `precondition=None`, `description=` set for the Ops Catalog) plus a
  `wiki_lint_handler(maker, ...deps)` factory returning a payload-only async
  `run(_payload)` — modelled on `analysis/hygiene.py:28-48`
  (`ENTITY_HYGIENE_SPEC` + `entity_hygiene_handler`). **Not** ridden onto the
  `WIKI_SPECS` tuple / `wiki_handlers()` dict: keeping the linter decoupled from
  `WikiBuilder`'s constructor keeps `test_wiki_builder_pg.py:500`
  (`assert set(wiki_handlers(...)) == {four keys}`) green untouched. The cost is
  explicit parity edits (enumerated in each wave's tests) — the same trade the
  three hygiene sweeps took.
- **`SYSTEM_CTX`, RLS-respecting.** Runs under the unscoped owner system context
  (`queue.SYSTEM_CTX`, `principal_kind='owner'`, `owner_scoped=False`, empty
  `domain_scopes`) exactly as `WikiBuilder` and `entity_hygiene` do — an
  unstamped job resolves to `SYSTEM_CTX` (`worker.py:79-88`). Under it
  `is_owner()` and `has_domain_scope()` are both true for every domain, so both
  RLS policy shapes (the graph tables' `has_domain_scope`-only and the `wiki_*`
  tables' `is_owner() AND has_domain_scope`) pass in one pass. **Because
  `SYSTEM_CTX` disables RLS filtering, the firewall input must come from code**
  (see §5) — this is the single hardest constraint and gets a per-wave
  security/red-team gate, and (per the coverage fix) a **100% security-path test in
  the first wave that ships any cross-article correlation code (Wave A)**, not
  deferred to Wave B.
- **Seeded schedule + `manual=true` Ops trigger, disabled by default.** One
  migration chained off **the current migration head at branch-cut** (`0114` as of
  2026-07-03 — **re-read the head when the Wave A branch is cut and name the file
  head+1**, never hardcode `0115` in the task; an intervening migration would
  collide the number and stale the `down_revision`) seeds a one-step pipeline
  (`action:'wiki_lint', action_version:1, params:{}`), a schedule, and a
  `manual=true` trigger with fixed UUIDs — mirroring `0047`/`0066`. It **does
  not** touch `app.actions`. The `manual=true` trigger gives the Ops "Run now"
  control for free (`fire_trigger(..., require_manual=True)`). Ships **disabled**
  (like 0066); a follow-up enable migration (the `0047→0048` precedent) flips it
  on once the owner is satisfied — decided per §9.
- **Runs + logging for free.** Registering `wiki_lint` as a pipeline action
  yields the required `runs` row (`PipelineRunLog.record`, `ran_as='system'`,
  `domain_code=None`) and per-step token/log capture (`finalize_job_step`,
  `worker.py:231`) with **no new run-logging code.**
- **Idempotent, re-run-safe.** `fire_trigger` (nightly tick) and Ops "Run now"
  can both fire; every side output is dedup/upsert-safe — each `review_items`
  insert is preceded by an open-item dedup `SELECT` (the shipped pattern at
  `pipeline.py:1190-1213`), and the re-dirty is `SET wiki_built=false WHERE
  id=ANY(:ids) AND wiki_built`, a no-op on already-dirty rows.
- **The `wiki_built` re-dirty is a plain, safe primitive — but only convergent
  where the builder will re-derive the missing artifact WITHOUT depending on the LLM
  re-drafting a specific clause AND WITHOUT depending on the carrying section
  reappearing.** `entities.wiki_built` is the live dirty bit
  (`notes.wiki_built` is **vestigial — never set true**; `builder.py:311/829`; §2
  check 2). Three triggers already move it (see Thesis):
  `wiki_entity_self_dirty` fires only on identity-column changes and **deliberately
  ignores a `wiki_built`-only flip** (so the re-dirty never fights it);
  `wiki_dirty_entity_from_fact` and `wiki_dirty_entity_from_mention` heal fact and
  mention mutations automatically. **A re-dirty is therefore prescribed ONLY for a
  class where (i) no trigger already heals it, (ii) a rebuild re-derives the missing
  artifact deterministically, and (iii) the artifact lives on a section the next
  plan will reproduce.** Classes that fail any of these — a notable entity that
  re-sources to zero sections; a missing/stale `wiki_link` whose re-emission depends
  on the LLM re-drafting a clause (checks 4a, 4b, 5b); a red-link whose carrying
  section may be restructured away (check 5a) — are routed to a **Talk-log count**,
  never re-dirtied, because re-dirtying them re-runs the expensive LLM builder to no
  effect and the finding reappears next run forever.

## 2. The checks — signal, determinism, convergence, output

Each check names its concrete columns, whether it is SQL-deterministic or needs
the LLM, **which mutation leaves it un-healed by the triggers**, and — critically —
**whether re-dirty converges against the PRODUCTION (LLM) build path AND whether
the carrying section is guaranteed to reappear**, or whether the finding is a
non-convergent weak signal that must go to Talk instead. Wave placement is in §6.

### Check 6 — Citation-integrity dangling `[n]` marker — **DROPPED (unreachable / self-healing; drop is contingent on the fact-backed-citation invariant)**

- **Why dropped.** The earlier premise ("a chunk/note purge CASCADE-deletes a
  `wiki_citations` row but no trigger re-dirties the citing entity") is **false
  against the code.** Purge is note-level and runs
  `delete(Fact).where(Fact.note_id==note_id)` (`analysis/purge.py:94`); every such
  fact DELETE fires `wiki_dirty_entity_from_fact` (`0046:90-92`), re-dirtying
  `entity_id` **and** `object_entity_id`. Every builder citation is **fact-backed**
  — `_source` selects `f.id AS fact_id` from `JOIN app.chunks` and every `Claim`
  carries that non-NULL `fact_id` (`builder.py:523/557`), and `facts.chunk_id` is
  `ON DELETE SET NULL` (`0006:187`), so even a bare chunk removal issues an UPDATE on
  `app.facts` that fires the same trigger. Therefore **the dangling-`[n]` state is
  coupled to a re-dirty** and self-heals.
- **The drop is CONTINGENT on the fact-backed-citation invariant — which the schema
  does NOT enforce.** `wiki_citations.fact_id` is **nullable** (`0046:158`, "null =
  chunk-only claim") and `PlannedCitation.fact_id` is `uuid.UUID | None`. The drop
  holds only because the **current builder** always sets `fact_id=f.id`; a future
  chunk-only citation path would be CASCADE-deleted via `chunk_id` (ON DELETE
  CASCADE) on a chunk purge with **no** fact-DELETE to re-dirty, silently re-opening
  the dropped dangling-`[n]` class. **Defence (Wave A, T-A3):** a cheap guard test
  asserts **no builder path writes a null-`fact_id` citation** (walk every
  `PlannedCitation` a `_source`/`_assemble` build produces on a seeded corpus;
  assert `fact_id IS NOT NULL` for all). If that test ever goes red (a chunk-only
  path is introduced), check 6 is **re-scoped to exactly the chunk-only-citation gap**
  with a reachability test that constructs the state through real note/chunk
  mutations and asserts the citing entity's `wiki_built` is still true afterward —
  never by fabricating an orphan `wiki_citations` row.
- **The `_CITE_MARKER` over-match hazard is also mooted.** Had the check survived,
  applying `_CITE_MARKER = re.compile(r"\[\d+\]")` (`readstore.py:30`) to the
  **stored, linkified** `wiki_revisions.body` (built via `_linkify`, `builder.py:748`)
  would over-match a purely-numeric wiki-link anchor `[2](wiki:...)` for a
  digit-named entity — a false dangling marker. Dropping check 6 removes this trap;
  no numeric `[n]` diffing against `wiki_citations.seq` is performed anywhere.

### Check 5 — Orphan / red-link scan (deterministic, no-LLM) — **all sub-cases Talk-count only (non-convergent)**

- **Signal (a) — red-link-became-notable → Talk-count only (NON-convergent, was a
  re-dirty class, now DEMOTED).** A red-link (`wiki_links.to_article_id IS NULL`)
  whose `to_entity_id` **now** has an active article or now clears notability.
  **Convergence is NOT unconditional.** A rebuild replaces the source's
  `wiki_links` rows **only for sections that reappear in the new plan**
  (`_write_section` DELETE is per-section, `builder.py:788`, and runs only for
  `plan.sections`, `builder.py:455-456`; there is no section reconciliation). Under
  the **production** LLM rewriter the red-link's clause survives into a section only
  if the LLM drafts it AND grounding keeps it (`_ground` `rewriter.py:157`, links
  emitted only in the grounded `kept` loop `rewriter.py:221-256`). If grounding
  drops the clause or the heading is restructured, the red-link's section vanishes,
  the DELETE never fires, the stale red-link row **survives**, check 5a re-fires,
  and re-dirtying the source produces an **unbounded expensive rebuild loop**. The
  `StubRewriter` emits one stable section per domain (`builder.py:169-175`), so a
  stub-driven convergence test would pass while production does not converge — the
  exact false-convergence signal this plan rejects elsewhere. **Therefore 5a is a
  Talk-count-only weak signal (source entity row-ids), never re-dirtied.** The
  builder-side fix that WOULD make it convergent — reconciling/pruning orphaned
  sections on rebuild — is out of this plan's additive scope and is named as a
  deferred alternative (§11).
- **Signal (b) — stale-missing-inbound → Talk-count only (NON-convergent).** The
  **naive** "active article with zero inbound `wiki_links`" signal is **DROPPED**: it
  fires on every normal leaf entity (a flood), and re-dirtying the orphan can never
  create inbound links (those come from OTHER entities). The **narrowed** sub-case —
  zero inbound links **AND** ≥1 other live in-scope entity holds a relationship fact
  (`facts.object_entity_id = this_entity_id`, live) toward it — is likewise
  **NON-convergent for production**: the live LLM rewriter emits the inbound link
  only if it drafted and grounded the source's relationship clause
  (`rewriter.py:221-256`, `_ground` at `:157`); if it did not, re-dirtying the source
  re-runs the expensive builder to no effect and the finding recurs → unbounded loop.
  So **5b is a Talk-count-only weak signal (row-ids of the source entities), never
  re-dirtied.**
- **SQL.** Red-link-became-notable (5a) = `wiki_links WHERE to_article_id IS NULL`
  whose `to_entity_id` resolves to an active `wiki_articles` row (or clears the
  check-3 notability predicate); collect the **source** `entity_id`s to **count**.
  Stale-missing-inbound (5b) = anti-join the zero-inbound set against `facts f WHERE
  f.object_entity_id = e.id AND f.status IN ('active','superseded')`, keeping only
  entities WITH such a live source fact; **count only.** Both apply the per-arm
  `domain_code` filter of §5 **on the entity ROW** (not the mention row).
- **Boundary vs `wiki_prune` (reconciled).** Prune archives **only GONE-`entity_ref`**
  articles (`builder.py:389-415`); it does **not** cover zero-inbound orphans. So
  there is **no double-report** — this check deliberately does not fire on the
  prune-owned GONE-entity class.
- **Output.** 5a → Talk-log count only. 5b → Talk-log count only. **No re-dirty, no
  review card** either way.

### Check 3 — Coverage gaps (deterministic, no-LLM) — **Talk-count only, NOT re-dirtied, sectionless class suppressed by default**

- **Signal.** An entity clears the notability gate but has **no active article**.
- **Why re-dirty is FORBIDDEN here (non-convergence).** `is_notable()` is **not**
  the builder's article-creation gate. After notability, `_build_entity` runs
  `_rewriter.plan(sourced)` and, when `plan.sections` is empty, **marks the entity
  built and returns no article** (`builder.py:444-453`) — deliberate and permanent
  for entities notable only via mentions or whose facts were all dropped by the
  same-domain-chunk skip. Re-dirtying such an entity makes `wiki_refresh`
  (`cost_class='expensive'`, LLM rewriter) re-source, re-derive zero sections,
  re-mark built, produce no article — the finding **reappears on the very next lint
  run and re-dirties again, forever.** Coverage gaps are therefore **never
  auto-re-dirtied.**
- **Suppress the deliberate sectionless population by DEFAULT.** The dominant
  population of "notable but no active article" is the **deliberate, permanent**
  notable-but-sectionless class; counting it run after run is owner-visible noise
  that counts intended behaviour as "drift." So the count **by default requires ≥1
  published, non-excluded fact that survives sourcing** (i.e. the entity would yield
  ≥1 citable section) before an entity is counted a *real* gap. Only if faithfully
  mirroring the sourcing proves fragile does the plan fall back to a **single
  aggregate number** (not per-entity row-ids), so the deliberate zero-section
  population never dominates the Talk log.
- **SQL (parity, count-only).** Notability is computed in Python at build time:
  `is_notable() = (>=3 published facts) OR (>=2 distinct source notes)`. Published
  facts are counted **through the same `JOIN app.chunks c ON c.id = f.chunk_id`**
  the builder's `_source` uses (`builder.py:521`) — a fact whose `chunk_id` is
  NULL/unresolved is dropped from `sourced.claims` and thus from the `>=3` count —
  minus `wiki_source_exclusions` (`article_id IS NULL` global rows); notes = distinct
  over fact-notes `UNION entity_mentions.note_id`. The lint SQL **mirrors the
  `JOIN app.chunks` and the exclusion filters** (importing
  `NOTABILITY_MIN_FACTS`/`NOTABILITY_MIN_NOTES`, never hardcoding), then `LEFT JOIN
  wiki_articles ON entity_ref=e.id AND status='active' WHERE article IS NULL` and
  applies the ≥1-citable-section default filter above. Because check 3 is
  **Talk-count-only**, any residual SQL↔Python divergence at worst mis-counts and
  **never drives a rebuild**; the claim is therefore "**approximate, count-only**,"
  not "exact."
- **Test (mandatory).** (a) A **notable-but-sectionless** entity (notable via
  `note_count>=2`, zero surviving sections, `wiki_built=true`, no article) is **NOT
  re-dirtied** *and*, under the default filter, is **NOT counted** as a real gap
  — asserted directly. (b) A **parity** case: a notable-by-fact-count entity whose
  facts have **NULL/unresolved `chunk_id`** is dropped by the `JOIN app.chunks`
  mirror exactly as `_source` drops it (the SQL count matches `len(sourced.claims)`);
  an **excluded**-fact/note case is likewise mirrored.

### Check 4 — Missing cross-references (deterministic, no-LLM) — **all sub-cases Talk-count only (non-convergent)**

- **Signal & the convergence verdict.** Two sub-classes, **both non-convergent
  against the production LLM rewriter → Talk count only, neither re-dirtied:**
  - **(A) Stale link — relationship fact exists, link absent.** A live relationship
    fact (`facts.object_entity_id = B`, status active/superseded, on source entity A)
    whose current revision of A's article carries **no** `wiki_link` A→B. The earlier
    "re-dirty A, the builder emits a link for every claim with `object_entity_id`
    (`builder.py:169-175`)" proof cites the **StubRewriter**, **not** production. The
    live rewriter emits an A→B link only if the LLM drafted and grounded A's clause
    about B (`rewriter.py:253-256`, `_ground` at `:157`). If that clause was never
    drafted (or grounding dropped it), a rebuild will **again** not emit the link →
    the finding recurs → unbounded expensive rebuild. So **4a is a Talk-count-only
    weak signal**, never re-dirtied.
  - **(B) Bare co-mention, NO relationship fact.** A pair co-mentioned in a chunk/note
    that **never produced a relationship fact.** The builder emits a `wiki_link`
    **only** from a relationship-fact `object_entity_id`; a pure co-mention has no
    such claim, so a rebuild re-derives zero links for the pair. The only real fix
    mints a **relationship fact** — graph mutation the linter must not perform. So
    **4b is a Talk-count-only weak signal (row-ids)**, never re-dirtied.
- **SQL.** Co-mention pairs = `entity_mentions` self-join on `chunk_id` (tight) or
  `note_id` (loose), `e1.entity_id <> e2.entity_id`, anti-joined against the
  link-existence predicate (`wiki_links l JOIN wiki_sections s ON
  s.id=l.from_section_id JOIN wiki_articles fa ON fa.id=s.article_id WHERE
  fa.entity_ref=A AND l.to_entity_id=B`). Partition the anti-join by whether a live
  relationship fact A→B exists: **fact present → 4a count; fact absent → 4b count.**
  Inverse/symmetric reciprocal asymmetry (via
  `supersession.INVERSE_PAIRS`/`SYMMETRIC_PREDICATES`) is a **weak** signal (minting
  the reciprocal is graph mutation) → Talk count, never a card, treating
  `inverse_predicate → None` as "no reciprocity expected."
- **Cost trap.** The self-join is O(pairs) and explodes on densely co-mentioned
  notes — needs a co-mention frequency threshold, DISTINCT-pair dedup, exclusion of
  already-linked and reflexive pairs.
- **Cross-domain safety.** Every sub-case applies the per-arm `domain_code` filter of
  §5 to **both entity ROWS** (join each mention to its `entities` row; filter on
  `entities.domain_code`, NOT `entity_mentions.domain_code`) before the pair enters
  any Talk count — this is a Wave A security path (see §5, T-A3). Because this
  self-join is a **direct 2-arm join** (both endpoints are the joined mention rows),
  the per-arm filter fully governs it — there is no multi-hop transitivity to leak
  through (contrast check 1's traverse path, §5).
- **Test (mandatory).** **Nothing in check 4 is re-dirtied** — a fact-backed missing
  link (4a) and a bare co-mention (4b) both go to the Talk count only and are asserted
  **absent from the re-dirty set**; already-linked + reflexive pairs excluded.
- **Output.** Talk-log counts only. **No human card, no re-dirty.**

### Check 2 — Stale-claim audit (Wave B, LLM verdict only)

- **Signal.** An article's prose **frames a superseded fact as CURRENT** (the
  defect) vs narrates it as history (fine). This is invisible in fact status — an
  LLM judgment — and keys on `wiki_articles.entity_ref → entities.wiki_built`, NOT
  `notes.wiki_built`. **This check is single-entity / single-article** — one
  article framing its own superseded fact as current — so a `wiki_stale_claim`
  finding has exactly **one** domain, not two.
- **The deterministic retracted-fact candidate is DROPPED (over-build).** Retraction
  is a plain UPDATE on `app.facts`, which **always** re-dirties via
  `wiki_dirty_entity_from_fact`; "cites-retracted-fact-with-`wiki_built`-true" is
  essentially unreachable, so no deterministic Wave-A slice of check 2 is built.
  Check 2 is therefore **entirely Wave B**.
- **Phase (LLM verdict).** For a superseded-fact-still-cited article, an LLM
  adjudicates whether the prose frames it as current. Shares the contradiction
  verifier's prompt/budget (§4). A **superseded** candidate is only considered when
  the single-head **moved** while `wiki_built` stayed true (walk `superseded_by`),
  never on `status='superseded'` alone.
- **Domain stamping (single-entity — NOT `card_domain`).** A `wiki_stale_claim` card
  is stamped with the **single subject entity's own** `entities.domain_code`
  (`article.entity_ref → entities.domain_code`) — **never** `card_domain(d_a, d_b)`
  (which is for the two-domain cross-article case), and **never**
  `ratchet_domain`/`_review_card_domain` (the order-dependent-leak helpers §5
  forbids for wiki cards). Passing the same domain twice into `card_domain` happens
  to return the shared domain, but the single-entity contract is stated explicitly
  here so a reviewer does not reach for the forbidden helpers. **Test (T-B3):** a
  check-2 card on a health entity is stamped `'health'` and is **invisible to a
  general-only scope**.
- **Output.** "Framed as current" → review card (Wave B). Where the article is at
  fault → ELEVATED `owner_correction` (§3); where the graph is already right and the
  article is merely stale → re-dirty / `request_rebuild`.

### Check 1 — Cross-article contradictions (LLM verifier, Wave B, ships last)

- **Signal.** Conflicting **live** claims across articles for the same/linked
  entities. No dedicated column — it compares live facts (via
  `wiki_citations.fact_id`) or section prose across two linked/co-mentioned
  entities.
- **Deterministic pre-filter (candidate generation) — fail-closed on the firewall
  BEFORE any LLM call.** Restrict to pairs where a single-head contradiction is even
  *possible*, reusing the pure `supersession` primitives: `GROUP BY (entity_id,
  predicate, qualifier) HAVING >1 row` that is `status='active' AND valid_to IS NULL
  AND assertion IN CURRENT_ASSERTIONS ('asserted','negated')`, restricted to
  **single-head kinds** (`is_functional`/`FUNCTIONAL_PREDICATES` +
  state/attribute/preference), then drop unit-equivalent restatements by calling
  **`_same_quantity(value_json, value_json)` on the two stored rows' `value_json`
  dicts** — NOT `values_equal`, which takes a `Candidate`/`FactView` pair
  (`supersession.py:341`), not two stored rows.
- **Candidate entity-pair generation is bounded and firewall-filtered at TWO seams
  — the per-edge filter alone is NOT sufficient (verified leak).**
  `neighborhood.traverse` (`analysis/neighborhood.py:209`) is a BFS that clamps
  `hops = max(1, min(depth, MAX_DEPTH))` (`:228`; `DEFAULT_DEPTH=2`, `MAX_DEPTH=3`,
  `:30-31`) and returns the **whole k-hop neighborhood set**, filtering only via the
  caller's `fetch_edges` callback. A per-edge filter enforced *inside* `fetch_edges`
  (`d_a==d_b OR d_a=='general' OR d_b=='general'`) is **per edge** and does **not**
  bound a multi-hop neighborhood: it admits the path `A(health)—edge—B(general)—edge
  —C(finance)` (edge A–B passes: one side general; edge B–C passes: one side
  general), so `C(finance)` lands in health-anchor `A`'s returned neighborhood, and
  forming the pair `(A,C)` produces the **(health, finance)** pair the fail-closed
  guarantee claims is impossible — a cross-firewall co-mingle. The `graph_context`
  precedent is safe **only** because it filters every row against **one fixed anchor
  domain** (`domain_code IN (:dom, 'general')`, `analysis/graph_context.py:221-285`)
  — a **star** filter that is transitively closed; a pairwise per-edge filter
  abandons that closure and is **not equivalent**. Therefore candidate-pair
  generation applies the firewall **twice**:
  1. **Pin `traverse` to `depth=1` for check-1 generation** (explicit `depth=1`
     argument — the library default is 2, so this MUST be stated), making each
     returned neighbor identical to a single anchor↔neighbor edge, so the per-edge
     `fetch_edges` filter fully governs the anchor↔neighbor pair.
  2. **Re-apply the firewall predicate AT PAIR-FORMATION on the two ENDPOINT
     entities' `entities.domain_code`** — for anchor↔neighbor pairs
     (`neighbor.domain == anchor.domain OR neighbor.domain=='general' OR
     anchor.domain=='general'`) and, if any neighbor↔neighbor pairs are ever formed,
     on **both** neighbors' rows. This reproduces the `graph_context` star filter's
     transitive closure at the pair boundary and is **robust regardless of traverse
     depth** — it is the mandatory fix; the depth pin is defence-in-depth and a
     candidate-volume reducer.
  The **co-mention self-join** applies the same per-arm predicate on each arm's
  `entities` row (a direct 2-arm join, no transitivity — §5).
- **Consequently a pair spanning two distinct RESTRICTED_DOMAINS (health×finance) is
  never generated**, so its two sides' claims/prose are **never concatenated into a
  verification prompt and never sent to the LLM adapter** — the fail-closed
  resolution of the cross-firewall co-mingle (§5). The Wave-B security-path test
  **drives the transitive general-bridge case** (`health—general—finance`) through
  the real generator at its configured depth, not only the direct co-mention path
  (§5, T-B2).
- **Bounded generation (concrete cap — see §4).** The anchor set is the entities
  surviving the single-head pre-filter **∪** entities holding ≥1 live cross-entity
  relationship fact; `traverse` is driven at `depth=1` over each; after DISTINCT-pair
  dedup + the pair-formation firewall filter, the candidate set is hard-capped at
  **`MAX_CANDIDATE_PAIRS = 500`** per run with a deterministic `ORDER BY
  (least(a,b), greatest(a,b))` so sampling is stable across runs.
- **Why LLM.** Tier-2 (unregistered) predicates carry no cardinality model and
  free-text `statement` fields can't be compared in SQL — the valuable case (two
  different in-scope-compatible entities' articles disagreeing, or two tier-2 claims
  conflicting) is intrinsically LLM-shaped.
- **LLM shape.** Reuse the batched-verdict pattern (`_GROUND_SCHEMA`,
  `rewriter.py:75-91` — `{index, supported}` array, one call adjudicates many
  candidate pairs), metered through a fail-closed budget gate (§4).
- **Honest caveat (see §8).** Because `supersession.decide()` **plus the pipeline's
  post-decide guards** route most same-key conflicts to `pending_review` at write
  time, a *live* same-key contradiction is rare (narrow escape hatches:
  derived/primary asymmetry outside the reciprocal path `pipeline.py:2365-2377`,
  best-effort reciprocity gaps, cross-note independent writes, purge/retraction).
  The **valuable** yield is cross-*entity* semantic disagreement within
  firewall-compatible pairs. Wave B validates real corpus yield before
  over-investing.
- **Output.** Review card (`review_items`, new kind), rendered through existing
  blocks by default (§7), `domain_code` stamped by the dedicated `card_domain(d_a,
  d_b)` helper (§5), NEVER by `ratchet_domain`. Where the article is at fault →
  ELEVATED `owner_correction` (§3); where the graph is already right → re-dirty /
  `request_rebuild`.

### Optional (owner decision §9-6a) — Index-integrity signals (deterministic, no-LLM) — **the only re-dirty leg; scoped to reappearing sections**

`wiki_index` is 1:1 per section, builder-written only (no sync trigger). Three
cheap joins mirror the "self-heal silently didn't run" shape: (a) missing
`wiki_index` row for a live section; (b) `last_updated_at` older than the section's
current `wiki_revisions.created_at`; (c) `embedding_model` <> the builder's
**configured** `self._model` (read it, never hardcode). `_upsert_index` runs
**only** inside `_write_section` (`builder.py:809`), i.e. **only for sections present
in the new `plan.sections`**. So these classes are **convergent re-dirty ONLY for a
section that reappears in the next plan** — the dominant case for a stable,
still-notable entity that already has published sections. **The orphaned-section
population is a NON-convergent residue** (a heading dropped from a later plan but
never pruned keeps its stale/missing index row, which a rebuild will not touch) and
is **explicitly excluded**: the re-dirty is emitted only when the entity **still
yields ≥1 citable section** (the check-3 sourcing filter — the entity is not the
deliberate sectionless class), which bounds the re-dirty to entities whose sections
the next plan reproduces. **Folded into Wave A only if the owner opts in (§9-6a).**
If declined, **Wave A has no re-dirty leg** (pure Talk + `runs` audit) — stated
honestly rather than substituting a class that production does not converge on.
Tested (T-A3) with a **controllable rewriter stub** (not the stable `StubRewriter`):
one case where the corrupted-index section **reappears** on rebuild (converges) and
one where the stub **drops** the section on rebuild (asserted **excluded** from the
re-dirty set / not re-surfaced), so the "no expensive-rebuild loop" guarantee is
proven for the orphaned-section edge, not assumed.

## 3. Correction / self-heal channel per finding (the three-weight fork)

There are **three** channels with different weights; picking the wrong one means a
disputed-article correction fails to supersede the graph head. Each finding type is
mapped to exactly one — and, per §2, a finding is only routed to **re-dirty** when
its class is convergent against the **production** build path (does not depend on the
LLM re-drafting a specific clause) **and** its artifact lives on a section the next
plan reproduces:

| Finding | Channel | Weight / mechanism |
|---|---|---|
| Index staleness (optional §9-6a), scoped to reappearing sections | **Re-dirty** `entities.wiki_built=false` (subject entity) | Self-heal on next `wiki_refresh`; convergent because the index row is re-derived deterministically for sections the plan reproduces; orphaned-section residue excluded; no note, no card |
| Red-link-became-notable (check 5a), coverage gap (check 3), fact-backed missing-xref (check 4a), bare co-mention (check 4b), stale missing-inbound (check 5b), reciprocal asymmetry, cross-firewall exclusion count | **Talk-log count only** | Owner-visible weak signal; **never re-dirtied** (non-convergent against the LLM rewriter, section may be restructured away, or intended behaviour); no card |
| Owner-approved single-article fix (from a card or from Talk) | `request_rebuild` (agent tool `wikiwritetools.py:63`) → `wiki_rebuild {target: article_id}` | Article rebuild only, no new facts |
| Stale-claim "framed as current" / contradiction where **the article is wrong** | **ELEVATED `owner_correction`** — review-card `correct` verb → `POST /api/wiki/{id}/corrections`, **or** agent `file_correction` in Talk | Mints `owner_correction` note; force-supersedes + pins the head (implemented today: `arbiter.py:104-105`, `pipeline.py:302`, `extraction.py:94`, `supersession.py:251`) |
| Contradiction where **the graph is already right** | Re-dirty / `request_rebuild` | No correction note |

**DO NOT** route disputed-article findings to `propose_correction` (agent Proposal
→ `agent_note_executor`): that files a NORMAL `provenance='agent'` note that does
**not** out-argue the graph head. **PHASE6 §4 is stale** — it calls the elevated
`owner_correction` extraction path a not-yet-built Wave-0 prerequisite, but the code
implements it today. `wiki_lint`'s owner-correction resolutions are a real, working
fix, not blocked on a phantom prerequisite; the plan verifies against code, not that
doc, and Wave A's docs task corrects the PHASE6 §4 note.

**Reopen story.** `_reverse_effects` (`repo.py:1780-1876`) understands a fixed
effect vocabulary with **no** `wiki_built`/redirty/rebuild case. Default (§9
decision 4): re-dirty/rebuild resolutions resolve with **empty effects** and are
documented as a bare non-reversible re-queue (rebuild is self-reconciling). If the
owner wants them reopenable, a new reversible `redirtied` effect
(`{action:'redirtied', entity_id, prior_wiki_built}`) + a matching `_reverse_effects`
branch is added — scoped as a small Wave B sub-task.

## 4. LLM cost gating (Wave B only)

`cost_class` is **display-only** — read solely by the Ops Automations Catalog
(`automations.py:169,301`); no scheduler/queue/worker branches on it. It does
**not** meter spend. So:

- Wave A (all deterministic, no-LLM) sets `cost_class='cheap'` — truthful, no
  budget needed.
- Wave B (contradiction + stale-claim prose verdict + optional sampled
  grounding-drift) calls the LLM adapter and **must** meter through a **fail-closed**
  gate like `WikiBuildGate` (`wiki/budget.py`, kill-switch + per-day token budget;
  the `plan()` idiom checks before spend and `record_spend` after,
  `rewriter.py:108-121,175-177`). §9 decision 5: use a **separate lint budget key**
  (`wiki_lint_daily_budget`, sibling of `wiki_build_daily_budget` in
  `settings_store.py:322-343`) so a corpus-wide audit can't starve the nightly
  build's token budget. When Wave B is present, `cost_class='expensive'` to reflect
  intent in the Catalog.
- **Concrete token envelope the `wiki_lint_daily_budget` must absorb (so Wave B is
  not sized blind).** The expensive path is check 1's cross-entity prose
  adjudication (the same-entity single-head GROUP BY only catches SAME-entity
  conflicts, which §8 concedes are rare/empty because `decide()`+guards route them to
  review at write time — it does **not** narrow the valuable cross-entity case). So
  the worst-case is bounded by the pair cap, not the pre-filter: **`MAX_CANDIDATE_PAIRS
  = 500`** per run (after DISTINCT-pair dedup + the pair-formation firewall filter,
  deterministic `ORDER BY`), verified in batches of **`VERIFY_BATCH = 20` pairs per
  adapter call → ≤ 25 adapter calls/run**, each side's concatenated prose capped at
  the builder's existing per-section excerpt length. The budget key is sized to this
  500-pair worst case; the optional grounding-drift re-check is **sampled** within
  the same envelope. These are explicit numbers, not "cap and sample."

## 5. Security scope — the head-on treatment

**Session scope: `SYSTEM_CTX`, never narrowed.** A corpus-wide scan is only correct
under the unscoped owner system context (the exact context `WikiBuilder` uses). Any
narrowed/`owner_scoped` session would silently see only its own domains and
manufacture false orphans/coverage-gaps/contradictions. The two RLS policy shapes
diverge (graph tables `USING has_domain_scope(domain_code)` alone; `wiki_*` tables
`USING is_owner() AND has_domain_scope(domain_code)`; `wiki_articles`/`wiki_talk_*`
owner-only), but **both pass under `SYSTEM_CTX`**. The plan must **not** shard
per-domain, and must **not** assume a future narrowed reader that can see graph rows
can also see the coupled `wiki_*` rows (the `is_owner()` conjunct diverges).

**Because `SYSTEM_CTX` disables RLS filtering, the firewall input must come from
code** (the `analysis/graph_context.py:176-181` doctrine). Every cross-article
correlation query (checks 1, 4, 5) re-applies an explicit `domain_code` filter **on
each joined row — the entity ROW included, not just its facts or its mentions** (the
`_load_entity` LEFT-JOIN-leak precedent, `graph_context.py:237-255`: an out-of-scope
entity's name/kind must be dropped, not surfaced into a finding payload). A naive
cross-article `LEFT JOIN` under `SYSTEM_CTX` is a firewall bypass.

**The firewall key is `entities.domain_code`, NOT `entity_mentions.domain_code`.**
Verified: `entity_mentions.domain_code` is the note/chunk domain of the *mention*
(migration `0006:102`), which can diverge from the entity's own domain — a `'general'`
entity mentioned in a `'health'` note carries `mention.domain_code='health'` but
`entities.domain_code='general'`. The key that governs whether an entity's **identity**
may surface is the entity's **own** `entities.domain_code` (that is what governs its
`app.entities` RLS visibility), exactly as `_load_entity` filters on the entity row.
So every per-arm predicate **joins each co-mention / edge endpoint to its `entities`
row and evaluates `d_a`/`d_b` on `entities.domain_code`**; the mention's
`domain_code` may be used only as an **additional narrowing**, never as the sole key.
The Wave-A security-path test **drives the divergent case** (a `'general'` entity
mentioned in a `'health'` note) to prove the entity-row key is the one enforced.

**Fail-closed at candidate generation — no two-firewall content ever reaches the
adapter, and the guarantee holds for the TRANSITIVE traverse path, not only direct
co-mention.** The per-arm rule is `d_a == d_b OR d_a == 'general' OR d_b ==
'general'` (each `d` from the **entity row**), enforced **at the generating seam,
before any content is assembled**, at **two** places for check 1 (per the verified
transitive-leak fix in §2, check 1):
- Inside the **`fetch_edges` callback** that feeds `neighborhood.traverse` (per-edge),
  **AND** at **pair-formation on the two ENDPOINT entities' `entities.domain_code`**
  — because a per-edge filter alone does **not** bound a multi-hop neighborhood
  (`traverse` returns the whole k-hop set; the `A(health)—B(general)—C(finance)` path
  otherwise leaks `C` into `A`'s neighborhood). The pair-formation endpoint filter
  reproduces the `graph_context` star-filter's transitive closure and is the
  mandatory guarantee; `traverse` is additionally pinned to `depth=1` for check-1
  generation (library default is 2) as defence-in-depth.
- Inside the **co-mention self-join** (`entity_mentions` self-join), each arm joined
  to its `entities` row, as a WHERE clause on the two arms' `entities.domain_code` — a
  direct 2-arm join with no transitivity to leak through.

Therefore a pair spanning **two distinct RESTRICTED_DOMAINS** (health×finance) is
**never generated**, so its two sides' claims/prose are **never concatenated into a
contradiction-verification prompt and never sent to the LLM adapter.** Cross-firewall
contradiction detection is thus an **explicitly accepted blind spot** — the sweep
does **not** surface an LLM-confirmed cross-firewall contradiction, by construction.
**Two security-path tests (100%) exercise BOTH candidate sources**, so the test can
never pass green while the transitive leak is live: (i) the **direct** co-mention
self-join path (seed a health entity and a finance entity that co-mention and hold
conflicting single-head claims); and (ii) the **transitive general-bridge traverse
path** — seed `health — general — finance` (a `'general'` entity co-mentioned with /
relationship-linked to both a health and a finance entity, all holding conflicting
single-head claims), run the **real pair generator through `traverse` at its
configured depth**, and assert (a) **no (health, finance) pair is ever produced** and
(b) the faked adapter receives **zero** prompt containing both restricted sides.

**Where a finding's card lives (domain stamping) — a dedicated helper, NOT
floor/ratchet.**
- **The review card DOES store precomputed content — so the guarantee is about that
  content's provenance, not "row-ids only."** Verified: `review_items.payload`
  carries the **precomputed card fields** (`summary`, `snippet`, `outcomes`,
  `choices`) **alongside** the row ids, written at insert time under `SYSTEM_CTX`
  (`analysis/display.py` module docstring); the reviewer's later scoped read does
  **not** re-fetch that text. T-B3's display helper follows this shipped pattern. The
  required security guarantee is therefore **NOT** "bare row-ids only" (which is
  false of a real card); it is that **all stored `summary`/`snippet` content derives
  only from sections whose domain ∈ {`card_domain`, `'general'`}.** Because
  generation already excludes two-restricted pairs and a general↔restricted card is
  stamped into the **restricted** side (via `card_domain`), the stored snippet's
  audience is authorized. A **Wave-B security-path test (100%)** asserts this
  directly as a **defense-in-depth backstop independent of generation-exclusion**:
  for every written card, no `payload.summary`/`snippet` fragment originates from a
  section whose domain is outside `{card_domain} ∪ {'general'}`.
- **Cross-article (two-domain) findings** stamp `domain_code` via a **new dedicated
  helper** `card_domain(d_a: str, d_b: str) -> str | None`:
  - return the **shared** domain if `d_a == d_b`;
  - return the **restricted** side if exactly one side is `'general'`;
  - return **`None` → route to Talk / suppress, never `review_items`** if the two
    are **distinct restricted** domains.
- **Single-entity findings (check 2 `wiki_stale_claim`)** stamp `domain_code` with
  the **subject entity's own** `entities.domain_code` (`article.entity_ref →
  entities.domain_code`), NOT `card_domain` (there is no second domain). See §2
  check 2; tested in T-B3.
- **`_review_card_domain(predicate, note_domain)` and `ratchet_domain(extracted,
  note_domain)` MUST NOT be reused for wiki-card stamping (cross-article OR
  single-entity).** Verified (`pipeline.py:212-220`, `extraction.py:180-192`) they
  take a **single** predicate and a **single** note_domain — a cross-article
  contradiction between section-A (domain `d_a`) and section-B (domain `d_b`) has
  **neither**. Worse, forcing the two-restricted case onto `ratchet_domain` **leaks**:
  `ratchet_domain('finance', 'health')` returns `'health'` and the swapped
  `ratchet_domain('health', 'finance')` returns `'finance'` (verified) — an
  **order-dependent stamp that surfaces one restricted side's content to a reviewer
  scoped to the OTHER restricted domain** via the `has_domain_scope(domain_code)`
  policy on `review_items` (verified `0006:241-243`). `card_domain` returns `None`
  for that case, so **no `review_items` row is ever written** for a two-restricted
  finding — and, because generation already excludes such pairs (above), one is never
  even a candidate.
- A general↔single-restricted finding stamps **into** the restricted domain (only a
  health-or-broader scope sees it), in **both** source orderings — asserted by the
  security-path test.

**The Talk build-log stays domain-neutral, and — unlike the review card — its
no-leak guarantee rests on the content shape.** `wiki_talk_*` RLS is
`USING (is_owner())` (verified `0053:54-55/81-82`), and `is_owner()` is true for
**any** owner-kind principal regardless of `domain_scopes` — it does **not**
distinguish a full unscoped owner from a domain-narrowed owner session. So the Talk
no-leak guarantee does **not** rest on "only the full unscoped owner can see Talk."
It rests entirely on the discipline that **Talk content is counts + bare row-ids
only** — no titles, no bodies, no domain names — exactly the builder's existing
build-log precedent. The log correlates at the **section** level (which carries
`domain_code`) and writes only counts/kinds/row-ids. **If** cross-firewall
exclusions are counted at all (owner §9 decision 3), the count is derived **purely
from the deterministic pre-filter (co-mention / endpoint row-ids)** with **no LLM
verdict over co-mingled content**. A security-path assertion checks the **Talk**
payload shape (**row-ids only; no title/body/domain-name**) — this row-ids-only
assertion is scoped to the **Talk build-log**, where it is true, and is **not**
claimed of the `review_items` card (which legitimately stores precomputed content,
guarded instead by the provenance backstop above).

**No new table → no new RLS isolation test for a table.** The design is deliberately
additive to `review_items` + dirty bit + `runs` + Talk. **If** the owner later
chooses a dedicated `lint_findings` table (§9 decision 2), it inherits the **full**
CLAUDE.md #3 obligation: `domain_code NOT NULL REFERENCES app.domains(code)`, `ENABLE
+ FORCE ROW LEVEL SECURITY`, a `has_domain_scope` policy, **and** a per-table RLS
isolation test (`test_analysis_rls.py` pattern). The default avoids all of this.

**Security-path tests are split across the waves that introduce the risk — Wave A is
NOT exempt.** Wave A already ships checks 4/5's **code-side per-arm
`entities.domain_code` filter under `SYSTEM_CTX`** on the direct 2-arm co-mention
self-join — the highest-risk code class in the system, because under `SYSTEM_CTX`
this filter is the **only** firewall enforcement left. Per CLAUDE.md #5 (security
paths at 100%, tests in the same PR as the code), Wave A (T-A3) ships a **100%-
coverage security-path test** proving the per-arm entity-row filter drops an
out-of-scope entity row from the Talk build-log counts/row-ids (checks 4/5a/5b) —
**including the divergent case** where the entity's own `domain_code` (`'general'`)
differs from its mention's (`'health'`), proving the **entity-row** key governs.
Wave B (T-B2/T-B3) adds the **traverse-path** transitive firewall test (health—
general—finance, §5 above), the card-stamping/no-leak tests (`card_domain`
general×health → `health`, invisible to general-only in **both** orderings;
health×finance → **no** `review_items` row + Talk-shape assertion; the single-entity
check-2 stamp = subject's own domain), and the **stored-content-provenance backstop**
(no card snippet derives from a section outside `{card_domain} ∪ {general}`). Wave B
is **not** the first wave to test the `SYSTEM_CTX` code-side firewall filter, and the
transitive traverse leak is tested at exactly the wave (B) where traverse-based
generation ships.

**Cross-domain reads under `SYSTEM_CTX` are red-teamed per wave.** Every wave
touching correlation queries is scope-touching, so its per-wave gate includes the
security/red-team pass PROCESS.md mandates; it specifically red-teams the LEFT-JOIN
entity-row leak, the mention-vs-entity domain divergence, and (Wave B) the transitive
general-bridge traverse leak, the two-firewall prompt-assembly path, and the
stored-snippet provenance.

## 6. Waves

Per `docs/reference/PROCESS.md`: worktrees, per-task + per-wave adversarial review,
one PR per wave. The cheap **deterministic no-LLM** checks ship **first and
independently**; the LLM verifier comes later; any new review-inbox card kind is
checked against the three-mock GUI gate before its wave (Wave W0).

| Wave | Delivers | LLM? | New table? | GUI gate? | Size |
|---|---|---|---|---|---|
| W0 | Owner ratifies §9 (decisions 1–7); confirm no bespoke card is wanted (else queue three mocks). No PR; the `W0` header glyph is flipped ✅ **in the Wave A PR** | — | — | possibly | — |
| A | Fifth `ActionSpec` + deterministic no-LLM checks (5, 3, 4) → **Talk (all weak signals: 5a/5b/3/4a/4b/reciprocal) + runs only; re-dirty ONLY via optional index-integrity (§9-6a), scoped to reappearing sections**; null-fact_id citation guard test; seed migration (disabled); lockstep edits; Wave-A security-path test; PHASE6 §4 doc fix | No | No | No | M |
| B | LLM verifier: contradiction (1) + stale-claim prose verdict (2) → `review_items` cards; two-seam firewall generation (pair-formation endpoint filter + depth=1) + transitive-path security test; candidate cap; `.prompt` + digest; lint budget gate; `card_domain` + single-entity stamping + no-leak + snippet-provenance tests | Yes | No | No¹ | M–L |

¹ No GUI gate **iff** findings render through existing blocks (default, §7). A
bespoke side-by-side contradiction card would trip the gate → the Wave W0
confirmation and, if chosen, a mock round precedes Wave B.

### Wave W0 — gates (no code, no PR)

Owner ratifies §9 in one pass before the Wave A branch is cut (PROCESS.md
open-decision escalation). Decisions **1, 3, 4, 5, 6** are hard-coded by the build
waves and cannot be treated as pre-approved. **Decision 6a specifically determines
whether Wave A has any re-dirty leg at all** — if declined, Wave A is a pure Talk +
`runs` audit (the honest consequence of demoting check 5a). Confirm the review-card
findings (Wave B) render through existing blocks; if the owner wants a bespoke
contradiction view, queue three interactive mock HTML artifacts
(`docs/reference/DESIGN.md` discipline) → chosen mock lands in `docs/mocks/` before
Wave B implementation. **Nothing is built in this wave.** Per DOC_LIFECYCLE line 107
the `W0` header glyph flip must land in a merged PR, so it **rides in the Wave A PR**
(T-A4), letting the freshness header reach all-✅ before the R4 archive check.

### Wave A — deterministic no-LLM slice

Conventional Commit:
`feat(wiki): wiki_lint corpus health sweep — deterministic no-LLM checks`

Behaviour after this wave: a fifth Ops-fireable/scheduled sweep runs the cheap
deterministic audits, writes a domain-neutral Talk build-log summary (the
**non-convergent weak-signal counts** — red-link-became-notable (5a), coverage gaps
(3), fact-backed missing-xref (4a), bare co-mentions (4b), stale missing-inbound
(5b), reciprocal asymmetry — deliberately **not** re-dirtied) + its `runs` row, and
— **only if §9-6a is opted in** — re-dirties the optional index-integrity signals
(scoped to reappearing sections) so the next `wiki_refresh` self-heals. If 6a is
declined this wave writes **no** re-dirty at all. No `review_items`, no new table, no
GUI surface, no LLM, no budget — the maximally additive first slice, and
independently shippable.

**T-A1 — `WIKI_LINT_SPEC` + handler skeleton + wiring (S/M).**
- Files: new `backend/src/jbrain/wiki/lint.py` (`WIKI_LINT_SPEC` +
  `wiki_lint_handler` factory, payload-only `run`); `backend/src/jbrain/worker.py`
  (import; append `WIKI_LINT_SPEC` to the `build_registry((*ACTION_SPECS, …))`
  tuple at ~:543 **and** add the `'wiki_lint'` handler to the `impls` dict);
  `backend/src/jbrain/main.py` (import; append `WIKI_LINT_SPEC` to
  `API_ACTION_SPECS` at ~:162).
- Tests (same PR): `backend/tests/unit/test_worker.py` — add `'wiki_lint'` to the
  composed handler-kind set literal (~:594, beside the wiki block); **mandatory** or
  the boot assertion fails. `backend/tests/unit/test_main_registry.py` — add
  `'wiki_lint'` to the `required` Ops-fireable set (~:15-35).
  `backend/tests/integration/test_scheduler_pg.py` — add `WIKI_LINT_SPEC` (and its
  import) to the `_registry()` build tuple (~:51-65) so scheduler-firing tests
  resolve the trigger (the standalone-shape parity edit). `test_wiki_builder_pg.py`
  and `test_actions_rls.py` stay **untouched** (standalone shape keeps them green).
- Non-negotiables: bijection holds (spec + handler added together, else worker boot
  raises `ActionRegistryError`); `mutating=True`; `cost_class='cheap'`; in-code only
  (no `app.actions`).

**T-A2 — Seed migration: pipeline + schedule (disabled) + `manual=true` trigger (S).**
- Files: new `backend/migrations/versions/<head+1>_seed_wiki_lint.py` where
  `<head+1>` and `down_revision` are **read from the current migration head at
  branch-cut** (`0114`/`0115` as of 2026-07-03 — **re-read at branch-cut; do NOT
  hardcode**, an intervening migration collides the number). Copies the
  `0047`/`0066` `_SEEDS` shape: one pipeline (`steps=[{action:'wiki_lint',
  action_version:1, params:{}}]`), one schedule, one `manual=true` trigger, fixed
  UUIDs; **does not touch `app.actions`**; symmetric `downgrade()`.
- Cadence + enabled flag per §9 decision 1 (default: nightly-after-prune interval —
  copy `0047` verbatim, `interval_seconds=86400`, `_next_run_sql(hour=4)` after
  prune's 03:45; ships **disabled**, `enabled=false`). A weekly cadence instead uses
  `schedule_kind='repeat'`, `schedule_freq='weekly'`, `schedule_days`,
  `schedule_time` (not `interval_seconds`) — getting this wrong yields a schedule
  that never fires or fires nightly. The seeded step **must** pin `action_version:1`
  to match the spec (`ScheduleResolutionError` guard, `scheduler.py:256`).
- Tests: migration up/down + a scheduler-firing test resolving the seeded trigger
  through `_registry()`.

**T-A3 — Deterministic checks + optional index re-dirty + Talk build-log + Wave-A security path (M).**
- Files: `backend/src/jbrain/wiki/lint.py` — the deterministic checks (5, 3, 4),
  each a read-only query under `scoped_session(maker, SYSTEM_CTX)`; write the
  domain-neutral Talk build-log summary (counts/kinds/row-ids) for **all
  non-convergent weak signals** (check 5a red-link-became-notable, check 5b stale
  missing-inbound, check 3 coverage gaps, check 4a fact-backed missing-xref, check 4b
  bare co-mentions, reciprocal asymmetry) as **counts only, not re-dirtied**. **The
  ONLY re-dirty** (emitted iff §9-6a = yes) is the **index-integrity** class, scoped
  to entities that still yield ≥1 citable section (orphaned-section residue
  excluded): collect those entity-ids and one `UPDATE app.entities SET
  wiki_built=false WHERE id=ANY(:ids) AND wiki_built`.
  Reuse: `readstore.py:201-222` (hub inbound-count query, inverted for the 5b
  weak-signal count), `readstore.py:140-147` (current-revision citation join if
  needed), imported `NOTABILITY_MIN_FACTS`/`NOTABILITY_MIN_NOTES` + the `_source`
  `JOIN app.chunks` + `_exclusions` filters (check 3),
  `supersession.INVERSE_PAIRS`/`SYMMETRIC_PREDICATES` (check 4 reciprocal weak
  signal). **`_CITE_MARKER` is NOT reused** (check 6 dropped).
- **Convergence discipline (mandatory):** NO deterministic check re-dirties. Re-dirty
  is emitted ONLY for the optional **index-integrity** class (§9-6a) and ONLY for
  sections the next plan reproduces (entity still yields ≥1 citable section).
  **Everything else — including check 5a (red-link-became-notable) — is Talk-count
  only, never re-dirtied**, because production may restructure the carrying section
  away (`_write_section` is per-section, `builder.py:788/455-456`; no section
  reconciliation) → an unbounded expensive rebuild loop. The convergence proofs
  reference the **production rewriter** (`rewriter.py:221-256` + `_ground` at `:157`)
  and the per-section write, never the `StubRewriter`.
- **Fact-backed-citation invariant guard (mandatory, defends the check-6 drop):** a
  test asserts **no builder path writes a null-`fact_id` citation** — build a seeded
  corpus through the real `_source`/`_assemble` path and assert every emitted
  `PlannedCitation.fact_id IS NOT NULL`. If it ever goes red, check 6 is re-scoped
  (see §2 check 6).
- Cross-domain safety: checks 4/5 cross-article joins re-apply per-arm `domain_code`
  filters **on the entity ROW** (`entities.domain_code`, the
  `graph_context`/`_load_entity` idiom), never on `entity_mentions.domain_code`.
  **This filter is marked a security path.**
- Optional index-integrity signals folded in **iff** §9 decision 6a = yes; if
  declined, this wave writes no re-dirty.
- Tests (`backend/tests/integration/test_wiki_lint_pg.py`, real Postgres, LLM never
  called; convergence sub-cases driven by a **controllable rewriter stub**, NOT the
  stable `StubRewriter`):
  - **check 5a** — a red-link whose target later got an active article is **counted
    to Talk and NOT re-dirtied** (asserted absent from the re-dirty set); explicitly
    asserted that the production non-convergent sub-case (carrying section dropped
    from the plan via a stub that omits it on rebuild) would NOT converge, which is
    why it is Talk-only.
  - **check 5b** — a zero-inbound leaf with **no** source relationship fact is **NOT**
    counted; a zero-inbound entity **with** a live source fact is **counted to Talk
    but NOT re-dirtied** (asserted absent from the re-dirty set).
  - **check 3** — coverage gaps: the mandatory §2 tests (notable-but-sectionless
    entity NOT counted under the default ≥1-citable-section filter **and** NOT
    re-dirtied; the `JOIN app.chunks` parity case where a NULL-`chunk_id` notable
    entity's SQL count matches `len(sourced.claims)`; an excluded-fact case).
  - **check 4** — a fact-backed missing-xref (4a) and a bare co-mention (4b) are
    **both counted to Talk and NOT re-dirtied**; already-linked + reflexive pairs
    excluded.
  - **index-integrity (iff 6a)** — a corrupted/missing index row on a section that
    **reappears** on rebuild is re-dirtied and **converges** (subsequent
    `wiki_refresh` re-writes the index row); a corrupted index row on a section the
    stub **drops** on rebuild (orphaned) is **excluded from the re-dirty set / not
    re-surfaced** (proves no expensive-rebuild loop for the orphaned residue).
  - **null-fact_id citation guard** — as above.
  - **re-dirty idempotency** — `AND wiki_built` is a no-op on re-run.
  - **no double-report** — prune-owned GONE-`entity_ref` articles are not surfaced.
  - **second-run stability** — a second lint run over an unchanged corpus re-dirties
    **nothing** (the Talk-only classes never enter the re-dirty set, and index-
    integrity converged), proving no expensive-rebuild loop.
- **Security-path test (100%, same PR):** the per-arm `entities.domain_code` filter
  in checks 4/5 drops an out-of-scope entity row from the Talk build-log
  counts/row-ids (5a/5b/4) — **driving the divergent case** where the entity's own
  `domain_code` (`'general'`) differs from its mention's (`'health'`), proving the
  **entity-row** key governs and the health row-id is not surfaced; the Talk payload
  carries row-ids only (no title/body/domain-name).
- Non-negotiables: no article write; runs on `SYSTEM_CTX`; no new table → no new
  per-table RLS test; no LLM; 80% coverage overall **and 100% on the marked
  correlation-filter security path.**

**T-A4 — Docs reconciliation + W0/A glyph flip (S).**
- This plan → **flip the existing ROADMAP `wiki_lint` entry** (filed at Scheduled
  under the dedicated `### Wiki health sweep (separate plan) — Scheduled` sub-section
  naming `docs/plans/WIKI_LINT_PLAN.md`) to *In progress* (transition 3); flip the
  header glyphs **`W0◻️→✅` and `A◻️→✅`** in this PR (per DOC_LIFECYCLE line 107 the
  W0 no-PR gate's flip rides here). `PHASE6_WIKI_PLAN.md` §4 note corrected (the
  `owner_correction` elevated path exists in code; `wiki_lint` is the new third leg —
  add a one-line pointer). `docs/reference/ANALYSIS.md` nightly-sweep list gains
  `wiki_lint`. Run `bash scripts/docs-freshness.sh`. **Do NOT create the ROADMAP
  entry here** — it was filed at Scheduled sign-off (transition 2). **Do NOT** move
  the entry under "Phase 6 follow-ons — Shipped" (that is T-B4 at archive).

### Wave B — LLM verifier (contradiction + stale-claim prose verdict)

Conventional Commit:
`feat(wiki): wiki_lint LLM verifier — cross-article contradiction + stale-claim review cards`

Behaviour after this wave: the sweep additionally runs the batched LLM verifier over
deterministically pre-filtered, **firewall-compatible** candidate pairs (two-distinct-
restricted pairs are never generated — via the two-seam filter, §5) and files
**human-judgment review cards** for genuine cross-article contradictions and
superseded-claims-framed-as-current, metered against a separate fail-closed lint
budget. Cards render through existing review blocks (no GUI gate) unless the owner
chose a bespoke card at Wave W0.

**T-B1 — `review_items` kind-CHECK migration (S).**
- Files: new migration extending `review_items_kind_check` with the new kind(s)
  (`wiki_contradiction`, `wiki_stale_claim`) via the shipped DROP+ADD pattern
  (`_BASE` + `_KINDS_WITH`/`_KINDS_WITHOUT`; `0032`/`0034` precedent); downgrade
  DELETEs rows of the new kinds first. **No new table**, rides the existing
  `review_items` RLS policy → no new per-table RLS isolation test.
- Tests: migration up/down; a row of the new kind inserts and reads back
  domain-scoped.

**T-B2 — Contradiction candidate generation + batched LLM verifier + budget (M).**
- Files: `backend/src/jbrain/wiki/lint.py` — deterministic pre-filter (single-head
  `GROUP BY (entity_id,predicate,qualifier) HAVING >1` live current-assertion rows;
  **`_same_quantity(value_json, value_json)`** unit-drop on the two stored rows'
  `value_json` dicts — **not** `values_equal`, which takes a `Candidate`/`FactView`
  pair). **Pair generation** drives `neighborhood.traverse` at **explicit `depth=1`**
  over the bounded anchor set (single-head survivors ∪ live-relationship-fact
  holders) with the per-arm firewall filter enforced **inside the `fetch_edges`
  callback AND re-applied at pair-formation on the two ENDPOINT entities'
  `entities.domain_code`** (the transitive-leak fix, §5), plus the co-mention
  self-join arm; DISTINCT-pair dedup + hard cap **`MAX_CANDIDATE_PAIRS = 500`** with
  deterministic `ORDER BY (least,greatest)`. Batched verifier via the adapter,
  `{index, supported}` schema mirroring `rewriter.py:75-91`, **`VERIFY_BATCH = 20`
  pairs/call**. New
  `backend/src/jbrain/wiki/prompts/wiki_lint_contradiction.prompt` with a `version`
  in frontmatter. New `wiki_lint_daily_budget` key (`settings_store.py` sibling of
  `wiki_build_daily_budget`) + a `WikiLintGate` (or reuse `WikiBuildGate` with the
  new key) — **check before spend, record after, break on exceeded, fail-closed** —
  sized to the 500-pair worst case (§4).
- Tests (same PR): `backend/tests/unit/test_promptfile.py`-style digest pin — ship
  the `.prompt` with its `version` **and** a hand-pinned sha256 test (both bump
  together or CI red). Verifier tested with the **adapter fake** (canned `{index,
  supported}` responses) — LLM never runs in tests. Budget gate: a run that exceeds
  the key stops before further spend (fail-closed). **Security-path (100%), BOTH
  candidate sources:** (i) direct co-mention — a seeded health×finance conflicting
  co-mention pair drives the generator and the faked adapter receives **zero** prompt
  containing both sides; (ii) **transitive traverse** — a seeded
  `health—general—finance` bridge (general entity linked/co-mentioned to both a
  health and a finance entity, all conflicting) driven through the **real generator
  at its configured depth** asserts **no (health, finance) pair is produced** and the
  faked adapter receives **zero** prompt containing both restricted sides. Cap: a run
  never generates more than `MAX_CANDIDATE_PAIRS` pairs. Unit-drop parity: a
  unit-equivalent same-key pair is dropped by `_same_quantity` (not sent).
- Non-negotiables: all LLM via the adapter (never a provider SDK); budget metered
  through the gate, not `cost_class`; `cost_class='expensive'` (display).

**T-B3 — Finding → review card + `card_domain`/single-entity stamping + resolution verbs (M).**
- Files: `backend/src/jbrain/wiki/lint.py` — a new `card_domain(d_a, d_b) -> str |
  None` helper (§5: equal→shared; one general→restricted; two-distinct-restricted→
  `None`) for **cross-article** findings — **NOT** `_review_card_domain`/
  `ratchet_domain`; a **single-entity** stamp for `wiki_stale_claim` = the subject
  entity's own `entities.domain_code` (`article.entity_ref`), **not** `card_domain`.
  File each surviving finding as `ReviewItem(kind='wiki_contradiction'|
  'wiki_stale_claim', payload={row-ids + precomputed summary/snippet + choices},
  domain_code=<card_domain(d_a,d_b) for cross-article | subject-entity domain for
  stale-claim>)` — cross-article only when `card_domain` is not None — preceded by
  the open-item dedup `SELECT` (`pipeline.py:1190` pattern); a `None` result (two
  distinct restricted — which generation already excludes) yields **no** `review_items`
  row and, if counted at all, a bare deterministic co-mention count in the owner-only
  Talk build-log (§5). New display helper in
  `backend/src/jbrain/analysis/display.py` returning the **precomputed**
  `summary`/`snippet` + `payload.choices[]` verbs (`dismiss`, `correct`,
  `request_rebuild`) — following the shipped precomputed-card pattern. New
  `_apply_resolution` branch(es) in `backend/src/jbrain/analysis/repo.py`
  (`(kind, action) → (status, effects)`): `dismiss`/`correct` reuse existing branches
  verbatim; a re-dirty/rebuild resolution resolves with **empty effects** (§3 reopen
  default). Frontend: add the new kinds to the `ReviewKind` union
  (`frontend/src/api/client.ts:765`), add SEQUENCE rows of existing blocks
  (`frontend/src/review/blocks/registry.ts`), list them in `registry.test.ts` — **no
  new React component** (no mock gate) unless Wave W0 chose a bespoke card.
- Tests (same PR):
  - **Verb round-trip** (the only unchecked contract — `/resolve` does not validate
    `(kind, action)`): the frontend `payload.choices` verb strings match the backend
    `_apply_resolution` branch exactly; an unknown verb → HTTP 400 `UnknownAction`.
  - **Cross-domain stamping (security path, 100%):** `card_domain('general',
    'health')` stamps `health` and is **invisible to a general-only scope** and
    visible to a health-or-broader scope in **both** source orderings
    (`card_domain('health','general')` too); `card_domain('health','finance')`
    returns `None` → produces **no** `review_items` row and is counted (if at all) in
    the owner-only Talk log with **row-ids only, no title/body/domain-name**; the
    Talk-content-shape assertion runs alongside the `review_items` checks.
  - **Single-entity stamping (security path, 100%):** a `wiki_stale_claim` card on a
    health entity is stamped `'health'` (via `article.entity_ref → entities.domain_code`,
    **not** `card_domain`/`ratchet_domain`) and is **invisible to a general-only
    scope**.
  - **Stored-content provenance (security path, 100%):** for every written card, no
    `payload.summary`/`snippet` fragment originates from a section whose domain is
    outside `{card_domain} ∪ {'general'}` (cross-article) or outside `{subject-domain}
    ∪ {'general'}` (single-entity) — a defense-in-depth backstop independent of
    generation-exclusion (drive it with a general↔health card and assert the snippet
    carries only general/health section text, never a third domain's).
  - Dedup: a re-run does not duplicate an open card.
  - Reopen: an empty-effects resolution reopens as a bare re-queue (documented).
- Non-negotiables: card stores precomputed content whose provenance ∈ `{stamp,
  general}` (never inlined out-of-scope text); `card_domain` (cross-article) /
  subject-domain (single-entity) used for stamping (never `ratchet_domain`);
  security-path 100%.

**T-B4 — Docs reconciliation + archive (S).**
- This plan → `Shipped`; `git mv docs/plans/WIKI_LINT_PLAN.md docs/archive/`;
  terminal banner naming the seed migration + the review-card kinds as ship evidence;
  **move the ROADMAP `wiki_lint` entry from the `### Wiki health sweep (separate
  plan)` sub-section to `## Phase 6 follow-ons — Shipped`** (its status now Shipped)
  and carry any residual/deferred items to it; add the `docs/archive/README.md` row
  (under "Wiki (Phase 6)", beside
  `PHASE6_WIKI_GRAPH_CONTRACT`/`TALK_BOARD`/`HYGIENE_SWEEPS_PLAN`) and **remove** the
  `docs/plans/README.md` row — same PR (R4). `PHASE6_WIKI_PLAN.md` +
  `docs/reference/ANALYSIS.md` corrected for the LLM leg. Run
  `bash scripts/docs-freshness.sh`.

## 7. GUI gate posture

The block registry makes a new review kind a **frontend-only "declare a sequence"** —
add the kind to the `ReviewKind` union + a `SEQUENCE` row of **existing** blocks
(`header`/`trace`/`claim:notice`/`claim:diff`/`action`/`evidence`) + a
`registry.test.ts` entry. Per DESIGN.md this is a small in-place change to an existing
surface and **does not trip the three-mock gate**. The action buttons are data-driven
from `payload.choices[]`/`outcomes` (`payload.ts:113-197`) — zero bespoke frontend
code. Wave A adds **no** GUI surface at all (Talk + runs, and — iff §9-6a —
re-dirty). The **only** gate trigger is a bespoke contradiction card (e.g. a
side-by-side view) — surfaced at Wave W0 as an owner confirmation; if chosen, three
mocks precede Wave B and the chosen mock lands in `docs/mocks/`.

## 8. Eval / acceptance criteria

1. **Registration lockstep green.** Worker boots (`build_registry(...).
   dispatch_table(impls)` validates the `wiki_lint` bijection); `test_worker.py`
   handler-kind set, `test_main_registry.py` `required` set, and `test_scheduler_pg.py`
   `_registry()` all carry `wiki_lint`; `test_wiki_builder_pg.py` and
   `test_actions_rls.py` **unchanged and green** (standalone shape; `app.actions`
   untouched).
2. **Deterministic correctness (Wave A, CI, no model).** Each check's integration
   test passes on a seeded fixture: **check 5a** red-link-became-notable **counted to
   Talk, NOT re-dirtied** (asserted absent from the re-dirty set — production may
   restructure the carrying section away); **check 5b** a zero-inbound leaf with
   **no** source fact **not** counted, a zero-inbound entity **with** a live source
   fact **counted to Talk but NOT re-dirtied**; **check 3** coverage-gap default
   filter suppresses the notable-but-sectionless entity from the count **and** never
   re-dirties it, and the `JOIN app.chunks` / exclusion parity cases match
   `len(sourced.claims)`; **check 4** fact-backed missing-xref (4a) and bare
   co-mention (4b) **both counted, neither re-dirtied**, already-linked + reflexive
   excluded. No double-reporting of prune-owned GONE-`entity_ref` classes. **Check 6
   is DROPPED** (fact-trigger heals the dangling-marker state) with the **null-fact_id
   citation guard test** defending the invariant the drop rests on.
3. **Self-heal loop converges (no expensive-rebuild loop).** NO deterministic check
   re-dirties; the **only** re-dirty is the optional index-integrity class (§9-6a),
   scoped to sections the next plan reproduces — a corrupted index row on a
   **reappearing** section converges after `wiki_refresh` (proven with a controllable
   rewriter stub), and a corrupted index row on a **dropped/orphaned** section is
   **excluded** from the re-dirty set (asserted). **Check 5a (red-link), a
   notable-but-sectionless entity, a fact-backed missing-xref (4a), a bare co-mention
   (4b), and a stale missing-inbound (5b) are NEVER re-dirtied**, so no
   re-source→zero-artifact/section-dropped loop and no unbounded
   `cost_class='expensive'` churn can arise. A **second lint run over an unchanged
   corpus re-dirties nothing** (asserted). The re-dirty is idempotent (`AND
   wiki_built`).
4. **Runs + Talk outputs.** A scheduled/Ops fire produces a `runs` row (system, no
   domain) and a domain-neutral Talk build-log summary (counts/row-ids, no domain
   name, no out-of-scope title), including the **non-convergent weak-signal counts**
   (red-link-became-notable, coverage gaps, fact-backed missing-xref, bare
   co-mentions, stale missing-inbound, reciprocal asymmetry) that are deliberately
   not re-dirtied.
5. **Wave A code-side firewall (security path, 100%).** The per-arm
   `entities.domain_code` filter in checks 4/5 drops an out-of-scope entity row from
   the Talk counts/row-ids (5a/5b/4), **including the divergent case** (entity
   `domain_code='general'` vs mention `domain_code='health'` proves the entity-row
   key governs); the Talk payload carries row-ids only.
6. **LLM verifier (Wave B).** Contradiction/stale-claim cards file only for
   candidates surviving the deterministic (firewall-compatible) pre-filter
   (`_same_quantity` unit-drop) + LLM verdict; generation never exceeds
   `MAX_CANDIDATE_PAIRS`; the verifier is tested with the adapter fake; the `.prompt`
   digest is pinned; the lint budget gate is fail-closed; **no verification prompt is
   ever assembled from two distinct RESTRICTED_DOMAINS — proven for BOTH the direct
   co-mention path AND the transitive `health—general—finance` traverse path driven
   through the real generator at its configured depth.**
7. **Firewall card stamping / no-leak (Wave B security path, 100%).** `card_domain`
   stamps a general×health card to `health` and it is invisible to a general-only
   scope in **both** orderings; a health×finance finding produces **no** `review_items`
   row and is surfaced only owner-only with row-ids (no title/body/domain-name); a
   single-entity `wiki_stale_claim` on a health entity is stamped `'health'` via the
   subject's own domain (not `card_domain`/`ratchet_domain`) and is invisible to a
   general-only scope; **every written card's precomputed `summary`/`snippet` derives
   only from sections whose domain ∈ {stamp, `'general'`}** (the provenance backstop);
   `ratchet_domain`/`_review_card_domain` are **not** used for stamping; the verb
   round-trip (`payload.choices` ↔ `_apply_resolution`) is asserted.
8. **Idempotent / dedup.** A re-run (nightly tick + Ops "Run now") does not duplicate
   open cards or double-flip dirty bits.
9. **Corpus yield sanity (Wave B gate).** Before investing further, validate on real
   corpus data that live same-key contradictions and stale-framed claims actually
   occur within firewall-compatible pairs — if the deterministic slice of check 1
   yields nothing, the value is the cross-entity semantic case and the
   pre-filter/budget envelope (`MAX_CANDIDATE_PAIRS`) is sized to that (an
   owner-visible finding, not a silent scope cut; the cross-firewall blind spot is an
   accepted, documented gap, not a defect).

## 9. Open decisions for the owner (deduped; recommended default first)

> **Ratification gate (PROCESS.md).** These are owner-escalation items, NOT
> pre-approved by this doc — ratified in one pass at sign-off (Wave W0) before the
> Wave A branch is cut. Decisions **1, 3, 4, 5, 6** are hard-coded by the build waves.

1. **Cadence + enabled flag.** Default: **nightly-after-prune** interval (`86400s`,
   hour 04:00 after prune's 03:45 — copy `0047`), **shipped disabled**, flipped on by
   a follow-up enable migration (`0047→0048` precedent) once the deterministic slice
   is trusted. Alternative: weekly (`schedule_kind='repeat'`, `freq='weekly'`), or
   ship enabled (the no-LLM slice is safe to enable).
2. **Findings sink: `review_items` (+ dirty bit + Talk) vs a dedicated
   `lint_findings` table.** Default: **`review_items` + dirty bit + runs + Talk** — no
   new table, no per-table RLS obligation. Alternative: a dedicated domain-scoped
   `lint_findings` table for cross-run dedup/history — inherits the full CLAUDE.md #3
   RLS + isolation-test obligation **and** the `domain_code` problem for
   two-firewall findings (which `card_domain` resolves as `None` → suppress). Note
   this table is **also the prerequisite** for a bounded once-only re-dirty of the
   currently-demoted 5a class (§11). Recommend the default.
3. **Cross-firewall (health×finance) contradiction handling.** Default: **an accepted
   blind spot** — the pair is **never generated** (per-arm `entities.domain_code`
   filter in `fetch_edges` **+ pair-formation endpoint filter + depth=1** for
   traverse, and the co-mention join), so no two-firewall content ever reaches the
   LLM and no card is minted; **optionally** a bare deterministic co-mention **count**
   (row-ids only, no LLM) in the owner-only Talk log to make the blind spot visible.
   Alternative: decompose into two per-domain cards each naming only its own side
   (more UI, per-side dedup complexity, and re-introduces the co-mingle risk at
   generation). Recommend the default; `card_domain` returns `None` for this case so
   `review_items` is never written.
4. **Reopenability of re-dirty/rebuild resolutions.** Default: resolve with **empty
   effects** — a bare non-reversible re-queue (rebuild self-reconciles), documented.
   Alternative: add a reversible `redirtied` effect + `_reverse_effects` branch storing
   `prior_wiki_built` (small Wave B sub-task). Recommend the default.
5. **LLM budget for Wave B.** Default: a **separate** `wiki_lint_daily_budget` key
   (fail-closed `WikiBuildGate`-style gate) so an audit can't starve the nightly build
   budget, sized to the `MAX_CANDIDATE_PAIRS=500` worst case (§4). Alternative: ride
   the existing `wiki_build_daily_budget`. Recommend the separate key.
6. **Index-integrity signals (the only re-dirty leg).** (6a) Default: **include** the
   three cheap `wiki_index` deterministic joins in Wave A — convergent re-dirty
   **scoped to sections the next plan reproduces** (the index row is re-derived
   deterministically per section; orphaned-section residue excluded via the
   ≥1-citable-section guard) and no trigger heals them. **Because check 5a was
   demoted to Talk-only (production may restructure the carrying section away),
   declining 6a leaves Wave A with NO re-dirty leg — a pure Talk + `runs` audit.**
   Alternative: leave to `reindex()`/the build path. **(6b is no longer an owner
   toggle** — the coverage-gap sectionless-suppression refinement is the **default**
   for check 3; the only fallback is a single aggregate number if mirroring the
   sourcing proves fragile.) Recommend include for 6a.
7. **Bespoke contradiction card.** Default: **reuse existing blocks**
   (`claim:notice`/`claim:diff`/`action`) — no GUI gate, no mock round. Alternative:
   a side-by-side contradiction view → three-mock GUI gate before Wave B. Recommend
   the default; revisit if the reused layout reads poorly.

## 10. Risks (honest)

- **Non-convergence is the sharpest failure mode, and it is designed out by routing
  every LLM-drafting-dependent OR section-reappearance-dependent class to Talk.** The
  builder has TWO gates after notability (`is_notable()` **then** non-empty
  `plan.sections`, `builder.py:444-453`); links come **only** from clauses the LLM
  drafted and grounding kept (`rewriter.py:221-256`, `_ground:157`) — **not** the
  `StubRewriter`; and `wiki_links`/`wiki_index` rows are rewritten **only per section
  present in the new plan** (`_write_section`, `builder.py:788/809/455-456`) with **no
  section reconciliation** — so a section restructured away keeps its stale rows. A
  check that re-dirties a notable-but-sectionless entity, a fact-backed missing-xref,
  a bare co-mention, a stale missing-inbound, **or a red-link whose carrying section
  the LLM may restructure away (check 5a)** drives an **unbounded expensive-LLM
  rebuild loop**. Mitigation: **all of those are Talk-count only, never re-dirtied**
  (§2 checks 3/4a/4b/5a/5b), with direct tests asserting the re-dirty set excludes
  them and a second-run-stability test. The **only** re-dirty is the optional
  index-integrity class, **scoped to reappearing sections** (orphaned-section residue
  excluded and tested via a controllable stub that drops the section on rebuild); if
  declined (§9-6a), Wave A has no re-dirty leg — stated honestly. A convergent
  builder-side fix (reconcile/prune orphaned sections on rebuild) is out of additive
  scope and named as deferred (§11).
- **The triggers already heal most drift — auditing healed drift is over-build.** The
  0046 `wiki_dirty_entity_from_fact`/`_from_mention` triggers re-dirty on fact and
  mention mutations. Verified: a note purge runs `delete(Fact).where(note_id==...)`
  (`purge.py:94`) which fires the fact trigger, and every citation is fact-backed
  (`f.id AS fact_id`, `builder.py:523/557`), so the **dangling-`[n]` state is coupled
  to a re-dirty and self-heals**. Mitigation: **check 6 is DROPPED** and no
  deterministic retracted-fact slice is built; Wave A rests on the Talk counts (+
  optional index re-dirty). **The drop is contingent on the nullable-`fact_id` column
  never carrying a chunk-only citation in practice** — defended by the Wave-A
  null-fact_id citation guard test; if a chunk-only path is ever added, check 6 is
  re-scoped to that gap. (Dropping check 6 also moots the
  `_CITE_MARKER`-over-matches-linkified-body false-positive hazard — no `[n]` diffing
  is performed.)
- **`SYSTEM_CTX` is a firewall bypass if the code-side filters lapse — and Wave A
  already carries that risk; the traverse path adds a TRANSITIVE variant in Wave B.**
  A naive cross-article `LEFT JOIN` surfaces an out-of-scope entity row into a Talk
  count; filtering on the **mention** domain instead of the **entity** domain would
  surface a `'general'` entity under a restricted domain it does not carry; and a
  per-edge-only filter on `traverse` leaks the `health—general—finance` transitive
  pair into candidate generation (a per-edge filter does **not** bound a multi-hop
  neighborhood). Mitigation: per-arm `entities.domain_code` filters on the entity ROW
  **plus a pair-formation endpoint filter and `depth=1` pin for traverse** (§5),
  marked security paths, with a **Wave A 100% test driving the mention-vs-entity
  divergent case** and a **Wave B 100% test driving the transitive general-bridge
  path through the real generator**, plus the per-wave red-team.
- **Cross-firewall contradictions are a real, accepted blind spot.** Excluding
  health×finance pairs at generation (fail-closed, at both seams) means an
  owner-relevant cross-firewall inconsistency is **not** detected. This is deliberate
  — no single `domain_code` can carry such a card without leaking a firewall, and
  `is_owner()` does not distinguish a narrowed-owner session (§5). Documented as a
  gap, optionally counted (row-ids only) in owner Talk.
- **Domain stamping with the wrong helper leaks.** `ratchet_domain('finance',
  'health')` → `'health'` and the swap → `'finance'` (verified) — an order-dependent
  leak across a firewall. Mitigation: a dedicated `card_domain(d_a, d_b)` helper that
  returns `None` for two-distinct-restricted (cross-article), and the subject
  entity's own `entities.domain_code` for single-entity `wiki_stale_claim`;
  `_review_card_domain`/`ratchet_domain` are **forbidden** for wiki-card stamping,
  asserted by the both-orderings + single-entity security tests. **Note also the card
  stores precomputed content** — so a second backstop (Wave B, 100%) asserts every
  card's stored `summary`/`snippet` derives only from sections in `{stamp} ∪
  {general}`, independent of generation-exclusion.
- **Deterministic contradiction is largely a negative result.** `decide()` + the
  pipeline's post-decide guards route most same-key conflicts to review at write time,
  so a *live* same-key contradiction is rare. The valuable yield is cross-entity
  semantic disagreement, LLM-only, which can explode in cost without the tight
  deterministic pre-filter + bounded pair-generator (`MAX_CANDIDATE_PAIRS=500`,
  `depth=1`). Acceptance criterion 9 forces a real-corpus yield check.
- **False-positive surface.** A contradiction/stale check that ignores
  unit-equivalent values (dropped via `_same_quantity` on the two stored `value_json`
  dicts, **not** `values_equal`), closed intervals, superseded-as-history citations,
  non-functional multi-value accumulation, differing qualifiers, or non-current
  modalities floods the inbox. Each is actively suppressed by reusing `supersession`
  primitives.
- **Notability drift.** Check 3 recomputes `is_notable()` in SQL; any divergence
  reports phantom gaps. Mitigated by importing the constants + **mirroring `_source`'s
  `JOIN app.chunks` and exclusion filters** (a fact with NULL/unresolved `chunk_id` is
  dropped exactly as `_source` drops it), and by a parity test asserting SQL/Python
  agree — and, because check 3 is Talk-count-only, a residual divergence at worst
  mis-counts, it never drives a rebuild.
- **Unchecked verb contract.** `/resolve` does not validate `(kind, action)` — a
  mismatch surfaces only at runtime as HTTP 400. The verb round-trip test (T-B3) is
  the mitigation.
- **Overlap with `wiki_prune` / citation cascades.** Prune archives only
  GONE-`entity_ref` articles and does not cover zero-inbound orphans; the plan scopes
  lint to what prune/cascade miss (red-link-became-notable and the missing-link weak
  signals, all counted to Talk) and tests that prune-owned classes are not
  double-reported.
- **`mutating=True` vs "read-only against the wiki"** reads as a contradiction at a
  glance. Mitigated by the spec description stating the read-only-article guarantee
  explicitly; `mutating=True` is correct (it writes `review_items` + the dirty bit).
- **PHASE6 §4 is stale.** It calls the `owner_correction` elevated path a
  not-yet-built prerequisite; the code implements it. Verify against code; T-A4
  corrects the note.

## 11. Deferred (named, not dropped)

- **Stale-claim prose verdict on `superseded`-framed-as-current** and the
  cross-article **contradiction** verdict are Wave B (both need the LLM). There is
  **no** deterministic retracted-fact Wave-A slice (dropped — the fact-trigger heals
  it), and **no** dangling-`[n]` check 6 (dropped — the fact-trigger heals the
  purge-coupled state, contingent on the fact-backed-citation invariant).
- **Red-link-became-notable (check 5a) as a re-dirty class** — demoted to Talk-only
  because production may restructure the carrying section away (no section
  reconciliation on rebuild), making the re-dirty non-convergent. Revisited only via
  **either** (a) a builder change that reconciles/prunes orphaned sections on rebuild
  (out of this plan's additive scope), **or** (b) a bounded once-only cross-run
  re-dirty marker, which requires the §9-decision-2 `lint_findings` table to record
  the once-only state.
- **Fact-backed missing-link healing (checks 4a/5b as re-dirty classes)** — deferred
  indefinitely: not convergent against the production LLM rewriter. Revisited only if
  a deterministic "the LLM will re-draft this clause" discriminator, or the same
  bounded once-only re-dirty guard (requiring the `lint_findings` table), is designed.
- **Cross-firewall (two-restricted) contradiction detection** — an accepted blind
  spot (never generated, at both seams); revisited only if a firewall-safe per-side
  decomposition is designed (§9 decision 3 alternative).
- **Reversible re-dirty effect** (`redirtied` + `_reverse_effects` branch) — only if
  §9 decision 4 flips.
- **A dedicated `lint_findings` table** for cross-run history/dedup **and** as the
  prerequisite for the bounded once-only 5a/4a/5b re-dirty above — only if §9 decision
  2 flips; carries the full RLS + isolation-test obligation.
- **Bespoke contradiction card** (side-by-side view) — only if §9 decision 7 flips;
  gated on the three-mock GUI gate.
- **`wiki-restructure` Proposal routing** for restructure-worthy findings
  (merge/split bundles) reuses the already-allowlisted stubbed Proposal kind and
  routes to the entity-level merge/split decision (PHASE6 §3a); not built here.