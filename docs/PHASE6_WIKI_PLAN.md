# JBrain2 — Phase 6 (Wiki) Build Plan

> **Status (in progress):** research + two red-team passes done. Owner decisions
> settled: #1 article scope (cross-domain, domain-tagged sections, section existence
> hidden from out-of-scope viewers) and #2 revision storage (inline `text`). Remaining
> before build: the **UI mock gate** (#3) and the cross-stream **citation/delta-feed
> contract** with the entity-graph rebuild (#4/#5). Most of Phase 6 is graph-coupled and
> gated on that rebuild; the parallel-safe slice now is the article/revision/index shell,
> editorial config, and the read-only UI behind its mock gate.

The LLM-maintained wiki: notes → facts → entities → **machine-written articles**,
every claim citing a note, corrected only by out-arguing it with a correction note.
Plan doc only — no code lands from this file. (v2: revised after two red-team passes;
v1's "build the spine now" thesis and its soft-ref citation design were both wrong —
see §0 and §2.)

## 0. Framing: the graph-coupling line (honest version)

A separate work-stream is **rebuilding the salient-fact → entity-graph**. The crux:
**that rebuild changes the fact/entity *shape*, not just IDs** — what the citable unit
is, whether the `superseded_by` self-chain persists, whether "salient facts" become a
new projection. So the coupling is deeper than v1 admitted. Verified line:

- **Genuinely STABLE → buildable now:** notes, chunks, `domain_code` + the RLS
  firewall, the review-inbox / Proposal primitive (incl. the stubbed `wiki-restructure`
  kind), the embed/pgvector/RRF path, subjects/principals. These don't depend on the
  fact/entity shape.
- **GRAPH-COUPLED → gated on the rebuild:** anything that **references a fact/entity**
  or reasons about **active-head / supersession / citability** — i.e. the
  `wiki_citations` table, delta-detection, the builder's triage + cited rewrite, and
  citation enforcement. v1 mis-classified `wiki_citations` and the enforcement contract
  as "spine"; they are coupled and move to the gated waves.

**Hard cross-stream dependency (new):** before the coupled work can start, the rebuild
stream must **freeze a citation contract** — (a) the citable unit's shape (a stable id
+ a citability predicate + `domain_code`), and (b) a **reliable fact change-feed**
(see §3). These are the rebuild's deliverables; this plan consumes them. Recorded as a
gating dependency, not a hope.

**What this means:** the genuinely parallel-safe work now is the **article/revision
shell + index path + editorial config + the UI (behind its mock gate, on fixtures) +
the owner-correction path**. The **citation table + builder brain** wait for the rebuild
contract. This is narrower than v1 claimed, and that's the honest read.

## 1. What already exists (reuse, don't rebuild) — cited

- **Workflow engine** (Phase 5): add a `wiki_build` **in-code** ActionSpec (mirror
  `EVAL_RUN_SPEC`/`PURGE_ACTION` — no `app.actions` row) + a nightly schedule/trigger
  seed migration (mirror `0044`). **Migration number assigned at build time** (the
  rebuild stream is consuming numbers in parallel — don't pin one).
- **Embeddings + retrieval** (Phase 2): TEI `bge-small-en-v1.5` 384-dim, `app.chunks`
  pgvector HNSW + FTS, hybrid RRF k=60 (`search/service.py`, `search/repo.py`,
  vector(384)+HNSW per migration 0003). The wiki index reuses this exact path.
- **RLS + isolation-test pattern**: `ENABLE/FORCE RLS` + `has_domain_scope(domain_code)`
  policy + an isolation test per new table (mirror `test_lists_rls.py`).
- **LLM adapter**: `router.complete(task="wiki.rewrite", json_schema=…)` — never a SDK.
- **Storage abstraction**: optionally blob-store revision bodies by sha256.
- **Review inbox / Proposals**: `wiki-restructure` proposal kind exists (`0018_proposals.py`).
- **Frontend**: React+Vite+TS PWA. Reuse `FactCitation` (`analysis/bits.tsx`) and
  `MatchBadge` (`screens/SearchScreen.tsx` — note: NOT bits.tsx), the `EntityScreen`
  read-only full-screen paradigm, the `Sheet` composer. The **Wiki launcher tile is
  stubbed** (`Launcher.tsx:53`, `phase:"P6"`, disabled).

## 2. Storage (new tables) — cross-domain articles, domain-scoped sections

ARCHITECTURE.md §Wiki is binding: *"citations are **foreign keys** to facts/chunks —
enforced data, not markdown convention."* v1's soft-ref design inverted this and is
**dropped** (hard FKs). All RLS-scoped, isolation-tested.

**Article model (owner decision): one CROSS-DOMAIN article per subject/entity, its body
split into domain-tagged SECTIONS. The *section* — not the article — is the firewall,
RLS, revision, and index unit. A scoped principal cannot see an out-of-scope section's
content OR its existence (the row simply doesn't return); only the owner (all domains)
sees every section.**

**Buildable now (graph-independent table *shape* — no FK into facts):**
- **`app.wiki_articles`** (cross-domain): `id`, subject/entity anchor (see note),
  `title`, `slug`, `status` (active|merged|archived), `merged_into_id` (reversible),
  `created_at`, `updated_at`. NOT domain-scoped. Article visibility is *derived*: a
  principal sees an article iff ≥1 of its sections is in scope (owner sees all). *(The
  entity-anchor linkage is graph-coupled → set at article creation in the gated builder
  wave; the table shape is not.)*
- **`app.wiki_sections`** (the firewall unit): `id`, `article_id` (FK ON DELETE CASCADE),
  `domain_code` (FK, NOT NULL), `current_revision_id` (FK→revisions ON DELETE SET NULL),
  `seq`. **RLS `has_domain_scope(domain_code)` governs BOTH content and existence** — an
  out-of-scope section returns no row to a scoped session.
- **`app.wiki_revisions`** (append-only, **per section**): `id`, `section_id` (FK ON
  DELETE CASCADE), `seq`, `run_id` (FK→`app.runs`), `body` (**inline `text`, markdown**),
  `summary`, `created_at`. Immutable; a domain rewrite touches only its
  section's revisions.
- **`app.wiki_index`** (**per section**): `id`, `section_id` (FK), `domain_code`,
  `summary`, `summary_embedding vector(384)`, `embedding_model`, HNSW index. The
  domain-scoped match target.
- **Editorial config as data** (grounded — ARCHITECTURE.md:105-107): style guide,
  citation-density floor, split/merge thresholds — a settings row / small table.

**Gated on the rebuild (FKs into the frozen fact shape):**
- **`app.wiki_citations`**: `id`, `revision_id` (FK ON DELETE CASCADE), `fact_id`
  (**hard FK → app.facts(id) ON DELETE RESTRICT** — a cited fact can't be purged without
  rewriting the revision; or SET NULL + rebuild trigger — §6 Q), `note_id`/`chunk_id`
  (**hard FK ON DELETE CASCADE** — honor the note-deletion purge), `entity_canonical_name`
  (render **cache only**), `domain_code`. **Firewall in Postgres (non-negotiable #3):** a
  CHECK/trigger asserting `citation.domain_code = section.domain_code = facts.domain_code`,
  plus an isolation test that a scoped session can neither create nor read a cross-domain
  citation, and cannot observe an out-of-scope section at all.

## 3. The nightly builder (GRAPH-COUPLED — gated)

`wiki_build` action + nightly schedule (mirror 0044; the **schedule seed lands with the
builder, not before** — a no-op scheduled action would spam the run-log and present a
dishonest Ops "run now"). Pipeline (ARCHITECTURE.md §Wiki):

1. **Delta facts.** **The hard problem.** Facts have **no reliable change-feed**:
   `Fact` has only `created_at` (no `updated_at`), and a naive `created_at >= last_run`
   watermark **misses** in-place `valid_to` interval-close, in-place refresh, `pinned`
   toggle, `status→retracted`/held, **entity merge** (`merged_into_id` re-points facts),
   note-deletion purge (removals), and `resolution.changed` re-keying
   (supersession.py / events.py). **Requirement on the rebuild stream:** add
   `facts.updated_at` (touched on every in-place mutation) **or** emit fact-mutation
   events into `app.events`. The builder consumes that feed; it cannot be built reliably
   without it.
2. **Index match** — embed the delta cluster, RRF against `wiki_index` → candidates.
3. **Triage** — one cheap LLM call per cluster → update | create | split | merge | ignore.
4. **Cited rewrite** — `router.complete("wiki.rewrite", json_schema=…)`. **Citation
   enforcement (corrected):** every claim cites a fact that is **non-retracted and
   same-domain** — NOT "the active head." Historical/superseded facts MUST be citable
   (biographical claims; "superseded facts stay queryable for citation integrity",
   ARCHITECTURE.md:96), and accumulating predicates legitimately have **multiple
   co-equal current facts**. Post-validate fail-closed against this predicate.
5. **Split/merge** — stage a `wiki-restructure` proposal; **owner-approved via the
   review inbox** before enactment.
6. **Re-embed** changed summaries into `wiki_index`.

## 4. Correction-note loop — NEW owner-authored machinery (not a thin wrapper)

"Discuss this article," anchored to a revision, must produce an **owner-authored,
elevated-weight** correction note citing the disputed revision (ARCHITECTURE.md:117-120).
The existing `propose_correction` (`agent/proposaltools.py`) produces an **agent**-authored
note at **NORMAL** weight — the *wrong* path. So this is **new machinery**, not a wrapper:
- an **owner-authored** note path (not the agent proposal tool);
- a **revision-anchoring** mechanism (no column anchors a note to a `wiki_revision` today);
- **verify the elevated-weight extraction path even exists** (grep found no "elevated"
  in code; migration 0018 reserves elevated weight for owner corrections but the
  pathway may be unimplemented).

The note-creation + anchoring is graph-independent (it writes a note); its *effect* on
articles is downstream of the gated builder.

## 5. Read-only wiki UI — blocking mock gate, then graph-independent (on fixtures)

**PROCESS.md gate (binding, blocking): three interactive HTML mocks → owner picks one →
the chosen mock lands in `docs/mocks/` as the binding spec. A critical-decision
interruption.** This is its **own pre-wave step**, gating the UI wave — not a sub-bullet
of the schema wave (the two have nothing to do with each other and can't share one PR).
**No reuse waiver** (DESIGN.md: "every NEW surface gets the round, 'it's just a list' is
not a waiver") — even though it reuses `FactCitation`/`EntityScreen`/`Sheet`.

Full-screen read-only surface, amber/read-only tint, the stubbed Wiki tile. Renders
stored articles/revisions; entity chips → `EntityScreen`; citation hover-cards
(pointer-not-copy). Graph-independent — built against **fixture data**. **DoD includes
mock fixtures for default / empty / long-article / error / offline states.**

## 6. Open decisions — triaged by what they block

**Must settle BEFORE the now-safe work (Wave A schema/UI):**
1. **Article scope:** ✅ RESOLVED — **cross-domain article per subject/entity, body in
   domain-tagged sections; the section is the firewall/RLS/revision/index unit; section
   existence (not just content) is hidden from out-of-scope principals.** (§2.)
2. **Revision body storage:** ✅ RESOLVED — **inline `text` (markdown)**; blob storage
   is reserved for large content-addressed attachments, not short section text. (§2.)
3. **UI direction:** the mock-gate outcome (its own decision interrupt). ← *next.*

**Must settle WITH the rebuild stream BEFORE the gated work (builder/citations):**
4. **Citation contract:** the citable unit's frozen shape + the `fact_id` FK ondelete
   policy (RESTRICT vs SET NULL+rebuild-trigger).
5. **Delta feed:** `facts.updated_at` vs fact-mutation events (a rebuild deliverable).

**File now as a standalone bug (independent of Phase 6):** the correction-note weight
doc/code discrepancy (ARCHITECTURE "elevated" vs code "normal") — it affects agent
corrections in production today.

## 7. Waves (PROCESS.md: worktrees, per-task + per-wave adversarial review, one PR/wave)

- **Wave 0 — gates (no code):** the **mock gate** (3 mocks → owner pick → `docs/mocks/`);
  settle decisions #1–#3; open the cross-stream **citation/delta-feed contract** with
  the rebuild team (#4–#5). Wave 0 unblocks the rest.
- **Wave A — graph-independent spine (parallel-safe now, after #1–#3):** `wiki_articles`
  + `wiki_revisions` + `wiki_index` tables + RLS + isolation tests (against the STABLE
  `domain_code`/note/chunk provenance — the fact-firewall test is deferred to the citation
  wave); editorial-config-as-data; the `wiki_index` embedding path; the `wiki_build`
  ActionSpec **stub only** (no schedule seed yet).
- **Wave B — UI (after the mock gate):** the read-only reader on fixtures, citation
  hover-cards, entity-chip nav, the **owner-authored** "discuss this article" → correction
  path + revision anchoring. Graph-independent.
- **Wave C — builder brain (GATED on the rebuild contract #4–#5):** `wiki_citations`
  (hard FK + Postgres firewall CHECK + isolation test), delta-detection on the agreed
  feed, index-match triage, cited rewrite with the corrected citability predicate,
  split/merge via the review inbox, re-embed, **and the nightly schedule seed** (now it
  does real work).

**Out of scope (explicitly):** self-improvement Loops 2 (skill learning), 3
(durable-knowledge + predicate-canon), and 4 (prompt/tool self-edit + the 100%
adversarial suite) are **separate roadmap items**, each its own multi-wave plan —
unblocked *by* the wiki/correction spine, not part of this plan. (v1 wrongly folded them
in as a "Wave D," which broke one-PR-per-wave and hid the true size.)

## 8. Non-negotiables (CLAUDE.md)

Adapter-only LLM; storage abstraction; **RLS firewalls enforced in Postgres** (the
cross-domain citation CHECK/trigger, not app code — #3) + an isolation test per new
table; machine-written wiki, humans correct via correction notes only (#7 — the whole
design); tests-with-code 80%/security-100%; Conventional Commits + branch + per-wave PR
+ CI green; `dev-setup.sh` updated for any new dep/tool/step.

**Exit (ROADMAP):** a day of notes updates only the affected articles overnight, every
claim cites a note, and corrections happen by out-arguing the wiki with a correction note.
