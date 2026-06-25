# Hygiene sweeps (build plan)

Phase-6 follow-on (ROADMAP §"Phase 6 follow-ons", `docs/ANALYSIS.md` nightly list); relocated
out of the wiki build as its own plan (`docs/PHASE6_WIKI_PLAN.md` "Out of scope"). Three
**core-data maintenance** engine actions on the Phase-5 sweep pattern (the
`consolidate_predicates` template): in-code `ActionSpec`s, run under `SYSTEM_CTX`, seeded as
**disabled-by-default** nightly schedules that are **Ops-fireable** (`manual=true`). Executed
under `docs/PROCESS.md`.

> **Scope finding (owner-decided).** Research showed the three named sweeps differ sharply in
> value today: **entity hygiene** is a real gap (orphans from retraction/supersession leak past
> the per-note purge); **summary re-embedding** is mostly covered (`wiki_reindex`,
> `sync_predicates`) and only bites on an embed-model change, with `entities.summary` not yet
> populated; **tag consolidation** is premature (tags have no registry/embedding and aren't
> surfaced anywhere). The owner chose to build **all three** — entity hygiene and re-embedding
> as real/insurance work, tag consolidation built **lean** (deterministic normalization only, no
> premature registry — semantic merging is a named deferred follow-on).

## Shared posture (all three)

- **Not self-improvement — data maintenance.** None spends LLM tokens, so none needs a token
  budget; they are siblings of the reconcilers (mechanical cleanup), gated only by
  the schedule's **enabled flag** (ships off) and the owner's Ops control. `mutating=True`.
- **In-code spec, seeded schedule.** Each `ActionSpec` is composed into the worker registry at
  boot (not in `app.actions`, so the 0035 seed-lockstep holds); migration `0066` seeds a
  disabled nightly schedule + a `manual=true` trigger per action (mirrors `0047`).
- **`SYSTEM_CTX`, RLS-respecting.** Each runs under the system session (sees all domains, like
  `wiki_reindex`); the domain firewall holds at the DB for every read/write.

## The three sweeps

### 1. `entity_hygiene` — orphan delete (the real gap)

`analysis/hygiene.py` + `analysis/purge.sweep_orphaned_entities`. Deletes **every** provisional
entity matching the **exact** safe criteria the per-note purge already uses (`_orphan_conditions`,
factored out so the two can never diverge): `status='provisional'`, no `subject_id`, **zero**
mentions, **zero** facts (as subject or object), **zero** `distinct_from` edges, and **not**
pointed at by a merge tombstone. The per-note purge only visits a *deleted note's* candidate
entities, so an entity left empty by a **fact retraction or supersession** (not a note deletion)
is never cleaned — this sweep closes that gap. Nothing citable is ever touched: a confirmed or
subject-linked entity (the "Me" entity is sacrosanct), anything a surviving fact mentions/cites,
and tombstones are all excluded. Auto-delete is consistent with the existing on-purge behavior
(these are zero-content provisional orphans; deleting one removes nothing referenced). Pure SQL,
`cost_class="cheap"`, idempotent.

### 2. `reembed_stale` — re-embed stale-model rows (insurance)

`analysis/reembed.py`. Re-embeds the embedded rows with **no existing re-embed path** whose
`embedding_model IS DISTINCT FROM` the current model (or whose embedding is NULL): **entities**
(`summary`, when one exists; a NULL-summary entity has nothing to embed and is skipped).
`wiki_index` (via `wiki_reindex`) and `canonical_predicates`
(via `sync_predicates`) already self-heal, so they are deliberately **not** duplicated here. Uses
the **local embed container** (not the LLM router) → no tokens, no budget. **Bounded per run**
(`_BATCH=256` per target) so a big post-upgrade backlog spreads across nights; the
`IS DISTINCT FROM` predicate self-advances, so it converges and is idempotent at the tail.
`cost_class="standard"`.

### 3. `tag_consolidate` — canonicalize note tags (lean)

`analysis/tagconsolidate.py`. One set-based `UPDATE` over `note_analysis.tags`: lowercase,
collapse internal whitespace, trim, drop empties, de-duplicate, re-aggregate to a sorted-distinct
array, and rewrite only rows whose array actually changed. So `["Medication","medication ",
"MEDICATION"]` → `["medication"]` across the corpus. Deterministic, idempotent (a second run is a
no-op), pure SQL, `cost_class="cheap"`. **Deliberately conservative** — only exact-after-
normalization duplicates merge; **semantic** merging ("med" ↔ "medication") needs an embedding-
assisted tag registry that does not exist and is a **deferred follow-on** (tags are not yet
surfaced or searched, so building that registry now would be bloat).

## Tests

Integration (`test_hygiene_sweeps_pg.py`, real Postgres): entity_hygiene deletes a provisional
orphan, keeps a confirmed/subject entity and one with a surviving mention, and is idempotent;
reembed restamps a stale-model entity-with-summary and skips a current entity and a
NULL-summary entity; tag_consolidate folds case/whitespace duplicates, drops empties, and is
idempotent. The worker handler-set + the scheduler seeded-pipeline-set lockstep tests gain the
three new entries.

## Cross-cutting non-negotiables

All DB on `SYSTEM_CTX`/RLS-scoped sessions; the domain firewall holds at the DB; **no LLM tokens**
(no router calls — the re-embed uses the local embed container); entity deletion reuses the
**already-shipped, tested orphan criteria** verbatim (no new deletion policy); seeds ship
**disabled**, Ops-fireable; tests-with-code; Conventional Commits + one PR + CI green; no new deps;
`dev-setup.sh` unchanged (no new tool/dep).

## Deferred (named, not dropped)

Semantic/embedding-assisted **tag** merging (needs a tag registry + surfacing); re-embedding the
rows that already self-heal (`wiki_index`, `canonical_predicates`) is intentionally **not**
duplicated; LLM-driven duplicate-entity **merge proposals** are a *separate* roadmap item
(duplicate detection, owner-gated), distinct from this orphan/integrity hygiene.
