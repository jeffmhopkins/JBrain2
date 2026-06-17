# JBrain2 — Phase 6 (Wiki) Build Plan

> **Status (in progress):** research + two red-team passes done. Owner decisions
> settled: #1 article scope (cross-domain article; type-guided single-domain sections;
> section existence hidden from out-of-scope viewers), #2 revision storage (inline
> `text`), and #3 UI direction (Wikipedia-style prose reader with type-guided sections +
> numbered references — chosen mock `docs/mocks/wiki-reader-chosen-wikipedia.html`).
> Remaining before build: the cross-stream **citation/delta-feed contract** with the
> entity-graph rebuild (#4/#5). Most of Phase 6 is graph-coupled and gated on that
> rebuild; the parallel-safe slice now is the article/revision/index shell, editorial
> config (incl. per-type section templates), and the read-only UI.

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
(see §3). **Written up as a hand-off spec for the rebuild team:
`docs/PHASE6_WIKI_GRAPH_CONTRACT.md`.** These are the rebuild's deliverables; this plan
consumes them. Recorded as a
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

## 2. Storage (new tables) — cross-domain articles, type-guided single-domain sections

ARCHITECTURE.md §Wiki is binding: *"citations are **foreign keys** to facts/chunks —
enforced data, not markdown convention."* v1's soft-ref design inverted this and is
**dropped** (hard FKs). All RLS-scoped, isolation-tested.

**Article model (owner decisions): one CROSS-DOMAIN article per subject/entity that
reads like a Wikipedia article — an infobox of key facts, a prose lead, and
TYPE-GUIDED SECTIONS whose taxonomy comes from the article *type* (a Person → Career /
Personal life / Health / Finances; an Organization → History / Products / Leadership /
Finances; …) defined in editorial config, not code. Each section is SINGLE-DOMAIN — the
firewall, RLS, revision, and index unit. Sensitive domains naturally surface as their
own sections (Health, Finances); the builder routes a finance fact to Finances, never
into general "Career" prose. A scoped principal cannot see an out-of-scope section's
content OR its existence (the row simply doesn't return); only the owner sees every
section. Citations render Wikipedia-style: inline `[n]` → a References section listing
the source note per `[n]`. (Chosen reader mock: `docs/mocks/wiki-reader-chosen-wikipedia.html`.)**

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
  citation-density floor, split/merge thresholds, the **notability gate** (which
  entities earn an article — §6), **and per-entity-type WIKI GUIDES**: for each entity
  type (Person/Org/Place/Event/…), an ordered **section** template (each section with a
  default domain + an include-if rule), a **style** spec (voice/tense/lead), and hard
  **requirements** (every claim cites a note, omit uncited/empty, no speculation, dates
  in the note's local tz). The builder loads the guide for the article's type at rewrite
  time. A settings table, not code; owner-tunable. **Starter set:
  `docs/WIKI_TYPE_GUIDES.md`** (Person/Org/Place/Project/Event/Concept + a generic
  fallback) — seed it into the editorial-config table.

**Gated on the rebuild (FKs into the frozen fact shape):**
- **`app.wiki_citations`**: `id`, `revision_id` (FK ON DELETE CASCADE), `fact_id`
  (**hard FK → app.facts(id) ON DELETE RESTRICT** — a cited fact can't be purged without
  rewriting the revision; or SET NULL + rebuild trigger — §6 Q), `note_id`/`chunk_id`
  (**hard FK ON DELETE CASCADE** — honor the note-deletion purge), `entity_canonical_name`
  (render **cache only**), `domain_code`. **Firewall in Postgres (non-negotiable #3):** a
  CHECK/trigger asserting `citation.domain_code = section.domain_code = facts.domain_code`,
  plus an isolation test that a scoped session can neither create nor read a cross-domain
  citation, and cannot observe an out-of-scope section at all.
- **`app.wiki_links`** (wiki↔wiki links + back-links): `id`, `from_section_id` (FK ON
  DELETE CASCADE), `to_entity_id` (the mentioned entity — the stable anchor),
  `to_article_id` (FK, nullable — resolved when/if the target has an article), `anchor`,
  `domain_code`. Powers article→article linking, the **"what links here"** back-links
  list, and a **notability signal** (inbound link count). Graph-coupled (references
  entities) → gated. Back-links are RLS-filtered by `from_section.domain_code` (you only
  see links from sections you can read). Targets are article *shells* (cross-domain), so
  a link never exposes the target's out-of-scope sections.

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
   `create` is gated by the **notability gate** (editorial config); a sub-threshold
   entity stays link-target-only (no article).
3a. **Type + guide selection** — the article's type = the entity's kind; load that type's
   **wiki guide** (sections/style/requirements) to drive the rewrite.
4. **Cited rewrite** — `router.complete("wiki.rewrite", json_schema=…)`, following the
   type guide; **resolve mentions to wiki links** (`wiki_links`: a mentioned entity →
   its article if one exists, else a red-link to the entity page). **Citation
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

## 5. Read-only wiki UI — mock gate ✅ done; build against fixtures

**PROCESS.md gate (binding): three interactive HTML mocks → owner picks one → the chosen
mock is the binding spec.** ✅ **DONE** — three directions (`wiki-reader-a/b/c-*.html`)
were presented; the owner chose **A (prose), refined to read like Wikipedia**:
`docs/mocks/wiki-reader-chosen-wikipedia.html` — infobox, prose lead, **type-guided
sections**, and **Wikipedia-style numbered `[n]` citations → a References section**. Its
rationale is recorded in `docs/mocks/wiki-reader-README.md` and lands in `DESIGN.md` when
Wave B starts.

Full-screen read-only surface, amber/read-only tint, the stubbed Wiki tile. Renders
stored articles/sections/revisions; **wiki→wiki links** (a mentioned entity opens its
*article* if one exists, else a red-link to its `EntityScreen`); `[n]` jumps to the
References list; a **"what links here"** back-links affordance; "discuss this article" →
the owner-correction path (§4). Graph-independent shell — built against **fixture data**.
**DoD includes fixtures for default / empty / long-article / error / offline states.**

## 6. Open decisions — triaged by what they block

**Must settle BEFORE the now-safe work (Wave A schema/UI):**
1. **Article scope:** ✅ RESOLVED — **cross-domain article per subject/entity, body in
   type-guided SINGLE-DOMAIN sections (taxonomy from the article type, via editorial
   config); the section is the firewall/RLS/revision/index unit; section existence (not
   just content) is hidden from out-of-scope principals.** (§2.)
2. **Revision body storage:** ✅ RESOLVED — **inline `text` (markdown)**; blob storage
   is reserved for large content-addressed attachments, not short section text. (§2.)
3. **UI direction:** ✅ RESOLVED — Wikipedia-style prose reader with type-guided
   sections + numbered references (`wiki-reader-chosen-wikipedia.html`). (§5.)

**Editorial-config tuning (data; sensible default now, owner-tunable anytime):**
6. **Notability gate:** what earns an article vs link-target-only. *Recommended default:*
   an entity with ≥3 cited facts OR referenced by ≥2 notes, restricted to the
   article-worthy type-families (Person/Org/Place/Project/Event/Concept); trivial
   one-offs stay plain entities. Lives in editorial config, so re-tunable without code.
7. **Link fallback:** ✅ RESOLVED — **wiki→wiki, with a red-link entity-page fallback**
   for entities that have no article yet (never a dead end). (§3 step 4, §5, `wiki_links`.)

**Must settle WITH the rebuild stream BEFORE the gated work (builder/citations/links):**
*Written up as a hand-off interface spec — `docs/PHASE6_WIKI_GRAPH_CONTRACT.md` (give it
to the rebuild team).*
4. **Citation contract:** the citable unit's frozen shape + the `fact_id` FK ondelete
   policy (RESTRICT vs SET NULL+rebuild-trigger). *Also covers `wiki_links.to_entity_id`
   resolution (mention → article) — entity id stability + merge/split re-point.*
5. **Delta feed:** `facts.updated_at` vs fact-mutation events (a rebuild deliverable),
   covering create/close/refresh/pin/retract/merge/purge/re-key.

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
