# JBrain2 — Phase 6 (Wiki) Build Plan

> **Status (in progress):** research + two red-team passes done. Owner decisions settled:
> #1 article scope (cross-domain article; type-guided single-domain sections; hidden
> out-of-scope sections); #2 revision storage (inline `text`, full-body → reconstructable
> diffs); #3 reader UI (Wikipedia-style — `wiki-reader-chosen-wikipedia.html`); the Talk
> board (chosen **B**, threaded topics); source depth (**B** — chunk-cited note-derived
> claims + grounding gate, entity graph wins on conflict); linking (wiki→wiki + red-link
> fallback); rebuild + source-exclusion controls; and the **`wiki_built` dirty bit** as
> the delta mechanism (replaces the change-feed). Remaining before the gated build: the
> cross-stream **citation contract + dirty bit** with the entity-graph rebuild (#4/#5).
> Most of Phase 6 is graph-coupled and gated on that rebuild; the parallel-safe slice now
> is the article/section/revision/index shell, editorial config (+ type guides), the
> `notes.wiki_built` bit, and the read-only UI + Talk board.

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
+ a citability predicate + `domain_code`), and (b) a **`wiki_built` dirty bit** on
entities (see §3 step 1). **Written up as a hand-off spec for the rebuild team:
`docs/PHASE6_WIKI_GRAPH_CONTRACT.md`.** These are the rebuild's deliverables; this plan
consumes them. Recorded as a
gating dependency, not a hope.

**What this means:** the genuinely parallel-safe work now is the **article/revision
shell + index path + editorial config + the UI (behind its mock gate, on fixtures) +
the owner-correction path**. The **citation table + builder brain** wait for the rebuild
contract. This is narrower than v1 claimed, and that's the honest read.

## 1. What already exists (reuse, don't rebuild) — cited

- **Workflow engine** (Phase 5): add the wiki **in-code** ActionSpecs (mirror
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
  `domain_code` (FK, NOT NULL), `parent_section_id` (**self-FK, nullable — subsections**:
  sections form a tree, H2→H3→H4), `current_revision_id` (FK→revisions ON DELETE SET NULL),
  `seq`. **RLS `has_domain_scope(domain_code)` governs BOTH content and existence** — an
  out-of-scope section (and its whole subtree) returns no row to a scoped session.
  **Subsections inherit their top-level section's domain** (no cross-domain nesting — the
  top-level domain section stays the firewall unit, its subtree hides together). A type
  guide may declare nested subsections; the builder may also auto-subdivide a long section.
- **Entity profile image (owner metadata, not a claim):** entities gain an owner-set image
  — `entities.image_sha` (blob-stored via the storage abstraction; sha256), uploaded in the
  **entity view**, **auto-rendered in the article infobox** (a person's photo, a business
  logo, a place's location shot). No citation (owner-provided, not note-derived); survives
  merges (keep the survivor's, owner may re-pick). Entity-keyed → graph-coupled (contract).
- **`app.wiki_revisions`** (append-only, **per section**): `id`, `section_id` (FK ON
  DELETE CASCADE), `seq`, `run_id` (FK→`app.runs`), `body` (**inline `text`, markdown**),
  `summary`, `created_at`. Immutable; a domain rewrite touches only its section's
  revisions. **Full history + diffs:** every revision keeps its full body, so the **diff
  between any two (incl. each build vs. its predecessor) is reconstructable** — a diff view
  needs no extra storage; optionally cache a short diff `summary` for the Talk Build-log.
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
- **`app.wiki_citations`**: `id`, `revision_id` (FK ON DELETE CASCADE), **`fact_id`
  (nullable** hard FK → app.facts(id) ON DELETE RESTRICT — a *fact-backed* claim; null for
  a *chunk-only* note-derived claim, decision §6 = B), **`chunk_id` (hard FK, NOT NULL** ON
  DELETE CASCADE — every claim cites at least a chunk; honors the note-deletion purge),
  `note_id`, `entity_canonical_name` (render **cache only**), `domain_code`. **Firewall in
  Postgres (non-negotiable #3):** a CHECK/trigger asserting `citation.domain_code =
  section.domain_code = chunk.domain_code` (and `= facts.domain_code` when fact-backed),
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
- **`app.wiki_source_exclusions`** (owner editorial suppression): `id`,
  `note_id` (nullable, hard FK, stable) **or** `fact_id` (nullable, hard FK, graph-coupled)
  — exactly one set, `scope` (`global` | a specific `article_id`), `reason`, `created_at`,
  `domain_code`. The builder filters these out of source-finding (step 3b). Owner-only RLS;
  audited. **≠ deletion** (source stays searchable) and **≠ retraction** (fact stays true)
  — purely "don't feature this in the wiki." The note-only rows are graph-independent (note
  ids are stable); fact-scoped rows are gated with the rest. An insert/delete here enqueues
  a `wiki_rebuild` of the affected article(s).

## 3. The builder (GRAPH-COUPLED — gated)

The builder runs as workflow-engine **actions** (in-code ActionSpecs, no `app.actions`
row — like `eval_run`/the reconcilers; pipelines + schedules + manual triggers seeded by
migration; every run a `runs` row). See **§3c** for the full action/trigger/budget map.
The two build entry points share the pipeline below:
- **`wiki_refresh`** — the **incremental** workhorse (dirty-bit driven; nightly schedule +
  Ops manual; schedule seed lands with the builder, not before — a no-op scheduled action
  would spam the run-log).
- **`wiki_rebuild`** — an **on-demand FULL rebuild** (non-delta), per-article *or*
  full-corpus, **manual-triggerable from Ops** (after a prompt/type-guide change, the
  entity-graph rebuild, or an exclusion edit). Skips the delta step (step 1), sources the
  full current set; `"all"` runs chunked + budget-aware. Both produce new revisions.

Both honor the **source-exclusion list** (§2 `wiki_source_exclusions`): notes/facts the
owner has purposefully suppressed are filtered out of source-finding (step 3b) — editorial
input-curation, distinct from deletion (still searchable) and retraction (still true). An
exclusion edit triggers a `wiki_rebuild` of the affected articles.

Pipeline (ARCHITECTURE.md §Wiki):

1. **Delta via a `wiki_built` dirty bit (mark-and-sweep — owner decision).** A boolean
   `wiki_built` on **notes** and **entities**, default **false at create and at edit**
   (and on fact change / entity merge / split / retract), set **true** once a build has
   incorporated it. The builder targets `wiki_built = false` rows and marks them true on
   success. This **replaces the brittle `created_at` watermark** — it cannot silently miss
   an in-place mutation (`valid_to` close, pin toggle, refresh, retract, merge), because
   *every write path flips the bit*. A **note edit also dirties the entities it mentions**
   (mention index), so decision-B chunk-only context is picked up even when no fact
   changed. The build target set = dirty entities (→ rebuild their articles) + dirty notes
   (re-extract / re-source). *(`wiki_built` on notes is graph-independent — Wave A; on
   entities it's the rebuild team's to maintain — contract §5.)*
2. **Index match** — embed the dirty cluster, RRF against `wiki_index` → candidate articles.
3. **Triage** — one cheap LLM call per cluster → update | create | split | merge | ignore.
   `create` is gated by the **notability gate** (editorial config); a sub-threshold
   entity stays link-target-only (no article).
3a. **Type + guide selection** — the article's type = the entity's kind; load that type's
   **wiki guide** (sections/style/requirements) to drive the rewrite.
3b. **Source-finding (decision §6 = B):** two-tier, per affected section's domain.
   *Backbone* = the entity's **citable facts** (`entity_id = E` or `object_entity_id = E`),
   each carrying its chunk citation — the arbitrated truth. *Context* = the entity's
   **mention-linked chunks** (`entity_mentions → chunks`, same-domain) — the source
   paragraphs, including detail that never became a fact. The builder writes prose from the
   **chunk text** (not just the terse fact statement), so claims can be *fact-backed* or
   *chunk-only*. (This is the "entity + note search" — bounded via the mention index, not
   an open trawl; an optional semantic chunk search can supplement.) **Then filter out any
   note/fact on the `wiki_source_exclusions` list** (global, or scoped to this article)
   before rewriting — the owner's purposeful suppressions never reach the prose.
4. **Cited rewrite + grounding gate** — `router.complete("wiki.rewrite", json_schema=…)`,
   following the type guide; **resolve mentions to wiki links** (`wiki_links`: a mentioned
   entity → its article if one exists, else a red-link to the entity page). **Citation
   enforcement:** every claim cites a **chunk** (and a fact when fact-backed) that is
   **non-retracted and same-domain** — NOT "the active head"; historical/superseded facts
   stay citable (biography; ARCHITECTURE.md:96), and accumulating predicates have
   **multiple co-equal current facts**. **Grounding gate (required, load-bearing under B):**
   a verifier asserts every sentence is **entailed by its cited chunk** and same-domain
   (reuse the eval-harness groundedness scoring + reflexion's citation check); fail-closed
   drops/flags unsupported prose. **Discipline — the entity graph wins on conflict:** facts
   are authoritative. The grounding gate checks each candidate claim against the entity's
   **current fact set** (not just its cited chunk); a chunk-only (note-derived) claim that
   **contradicts a current fact is dropped** — the fact prevails. Chunk-only claims may
   only add **non-conflicting** detail, and never resurface superseded/retracted content.
5. **Merge/split — follows the ENTITY graph (see §3a).** Article identity = entity
   identity, so a merge/split is driven by the entity-resolution decision (owner-approved
   in the review inbox), not invented by the wiki. The builder enacts the downstream
   article effect (redirect / re-partition), logs it to the Talk Build-log, and re-resolves
   links/citations. A builder-detected candidate surfaces as a `wiki-restructure` proposal
   that **routes to the entity-level** merge/split (so wiki and graph never fork).
6. **Re-embed** changed section summaries into `wiki_index`; **emit the per-article blurb**
   (the lead's 1–2 sentences, stored for the landing index + search); **mark the built
   notes/entities `wiki_built = true`** (close the mark-and-sweep loop).

## 3a. Taxonomy & merge/split (article identity = entity identity)

**Taxonomy is inherited, not invented.** One article per **notable entity** (the
notability gate); the article's **type = the entity's kind** → its **wiki guide** →
sections. No separate article-classification step; Wikipedia-style cross-cutting
**categories** are derivable later from type + the link graph (deferred).

**Merge** (entity A → B): B absorbs A's facts/sources (re-pointed by the merge); **A's
article becomes a redirect** to B (`status=merged`, `merged_into_id=B`; A's title/slug
become aliases that resolve to B — never deleted, so back-links + history survive). Both
entities marked dirty → rebuild B; Build-log "merged A into B". **Reversible** (un-merge
restores A's article from the redirect), mirroring the graph's reversible un-merge.

**Split** (entity X → X + Y): partition X's facts/mentions/sources by the new identities
(the resolver re-partitions mentions by span/provenance); rewrite X; **create Y's article**
if notable; move Y's claims over; re-resolve links/citations. Build-log "split Y from X".
Reversible.

**Approval & no-drift:** the **entity** merge/split is the single owner-approved decision
(review inbox); the article restructure is a downstream, logged, reversible build effect —
not a second approval. **Deferred:** purely-editorial length/topic splits (no entity
change) — rare at personal scale; merge/split stays entity-driven for v1.

## 3c. Actions & workflows (the engine map)

All four are **in-code ActionSpecs** (no `app.actions` row — like `eval_run`/the
reconcilers); pipelines + schedules + `manual=true` triggers seeded by migration; every
run is a `runs` row in the Ops Automations catalog. All GATED → Wave C.

| Action | What it does | Triggers | Class |
|---|---|---|---|
| **`wiki_refresh`** | Incremental, **dirty-bit driven**: build/update articles for `wiki_built=false` entities (+ notes→mentioned), enact merge/split redirects for dirtied entities, re-embed touched summaries, post the Build-log, mark clean. Cost scales with the day's changes. | nightly schedule + Ops manual + *(opt)* entity-change event | expensive · mutating · budgeted |
| **`wiki_rebuild`** | **Full re-derive** `{article_id\|"all"}`, ignores the dirty bit; new revisions; `"all"` is chunked + budget-aware. | Ops manual; auto-enqueued by an exclusion edit + prompt/guide version bumps | expensive · mutating · budgeted |
| **`wiki_reindex`** | Re-embed **all** `wiki_index` summaries after an embedding-model swap (mirrors `sync_predicates`). | Ops manual + on model change | standard · mutating(index) |
| **`wiki_prune`** | Archive/redirect **orphaned** articles (entity purged / below notability); GC (mirrors `purge_deleted_artifacts`). | nightly (after refresh) + Ops manual | cheap · mutating |

**Self-reconciling:** `wiki_refresh` is a dirty-sweep, so a dropped enqueue self-heals on
the next scheduled run — no separate reconciler action needed (a bonus of the dirty bit
over an event feed).

**Triggers / events:** nightly schedule → `wiki_refresh` → `wiki_prune`; Ops manual → any;
`wiki_source_exclusions` insert/delete → `wiki_rebuild(article)`; entity merge/split
resolution → entities dirtied → next `wiki_refresh`; correction note → ingest → dirtied →
`wiki_refresh`; embedding-model change → `wiki_reindex`.

**Budget:** a dedicated **wiki-build token budget** (mirrors the integration budget;
SEPARATE from the self-improvement/eval budget — wiki building is core maintenance, not
self-improvement). `wiki_rebuild("all")` chunks so a full re-derive can't blow a night's
budget. **Schedule placement:** ~03:30 UTC, after the 02:00 graph sweeps + 03:00 eval.

## 4. Editorial discussion board ("Talk") + the correction loop

Like a Wikipedia **Talk page**: a persistent, article-anchored **editorial conversation**
between the owner and the agent — the generalization of one-shot "discuss this article."
The wiki stays machine-written; Talk is the **conversational front-end** over the
sanctioned levers (correction note, source exclusion, rebuild, split/merge proposal).

**Two voices write the thread:**
- **The batch builder posts editorial-decision summaries** per build (Wikipedia's
  bot-on-Talk behavior): "split off Finances", "dropped 2 uncited claims", "excluded
  note X per your request", "merged …". Transparency into what changed and why.
- **The interactive agent converses** — the **Phase-4 chat agent** given wiki context +
  editorial tools (NOT the batch builder). It can **explain** the article (why a claim is
  there, which note/run/guide produced it) by introspecting the **build run + citations +
  type guide**, and it enacts outcomes.

**Reuse, not new infra:** the agent loop / tool-calling / Proposals / transcript store
(Phase 4). A Talk **thread = an agent session anchored to an `article_id`** (reuse
`agent_sessions` + transcript; the builder's decision posts append to the same thread —
durable editorial history). **New = a small set of wiki-editorial tools:**
`explain_article` / `get_sources` (transparency), `file_correction`, `add_source_exclusion`,
`request_rebuild`, `propose_split_merge`. Consequential actions are owner-approved via the
Proposals primitive. **Owner-only**, with the **firewall on the agent's reads** (it can't
surface a health source while discussing a general section).

**Correction-note path (one Talk outcome) — NEW owner-authored machinery, not a thin
wrapper.** A correction must be an **owner-authored, elevated-weight** note citing the
disputed revision (ARCHITECTURE.md:117-120). The existing `propose_correction`
(`agent/proposaltools.py`) produces an **agent**-authored note at **NORMAL** weight — the
wrong path. So this needs: an owner-authored note path; a **revision-anchoring** column
(none today anchors a note to a `wiki_revision`); and **verify the elevated-weight
extraction path even exists** (grep found no "elevated" in code).

**Coupling:** the thread storage + agent wiring + the owner-correction/anchoring are
graph-independent (Wave B). The **explain-sources depth** and the **builder's
decision-logging** read facts/citations → graph-coupled (gated, Wave C).

## 5. Read-only wiki UI — mock gate ✅ done; build against fixtures

**PROCESS.md gate (binding): three interactive HTML mocks → owner picks one → the chosen
mock is the binding spec.** ✅ **DONE** — three directions (`wiki-reader-a/b/c-*.html`)
were presented; the owner chose **A (prose), refined to read like Wikipedia**:
`docs/mocks/wiki-reader-chosen-wikipedia.html` — infobox, prose lead, **type-guided
sections**, and **Wikipedia-style numbered `[n]` citations → a References section**. Its
rationale is recorded in `docs/mocks/wiki-reader-README.md` and lands in `DESIGN.md` when
Wave B starts.

**Second surface — the Talk board (§4) — mock gate ✅ DONE.** Three directions
(`wiki-talk-a/b/c-*.html`) were presented; the owner chose **B — threaded topics** (true
Wikipedia Talk: collapsible topics with status badges, signed/timestamped replies, a "New
topic" action, and an auto **Build-log** topic for the builder's decision posts).
Rationale in `docs/mocks/wiki-talk-README.md`; lands in `DESIGN.md` when its UI is built.

Full-screen read-only surface, amber/read-only tint, the stubbed Wiki tile. Renders
stored articles/sections/revisions as encyclopedic prose with **nested sections (H2/H3/H4)**,
**bulleted lists + tables** (per the writing-style spec), the **entity profile image** in
the infobox, **wiki→wiki links** (a mentioned entity opens its *article* if one exists, else
a red-link to its `EntityScreen`); `[n]` jumps to the References list; a **"what links here"**
back-links affordance; "discuss this article" → the owner-correction path (§4).
Graph-independent shell — built against **fixture data**. (Worked example, all of the above:
`docs/mocks/wiki-reader-example-priya.html`.) **DoD includes fixtures for default / empty /
long-article / error / offline states.**

**Owner-only editorial affordances** (distinct from the read-only reader; gated to the
owner): **Rebuild** (fire `wiki_rebuild` for this article) and **Exclude this source**
(add a reference's note/fact to `wiki_source_exclusions`, then rebuild). These are
curation, not article-text edits — the wiki stays machine-written. They surface on the
reader (an owner "⋯" menu / a per-reference action) but enact in the gated builder wave.

## 5b. Wiki landing + search

**Landing — a *living* home, not a static index** (its own mock gate — pending). The Wiki
tile opens a **search-first** home with auto-curated rails, each entry a title + a 1–2
sentence **blurb** (the article's **lead** — already written by the builder; stored as a
per-article summary for the index + search, no new generation):
- **Search box** up top (the article-aware search below).
- **Recently updated** — what the last `wiki_refresh`/`wiki_rebuild` changed (from the
  Build-log / `runs`); a living wiki shows its pulse.
- **Most connected (hubs)** — articles with the most inbound `wiki_links` (the
  back-link/centrality signal) — the natural entry points, ranked for free from the graph.
- **Type-grouped index** — People · Organizations · Places · Projects · Events · Concepts,
  collapsible, A–Z within. The index as one rail.
- *(Optional later: a graph/map view of the link network as a secondary tab.)*

**Taxonomy is derived, never hand-maintained:** primary grouping = the **entity type**
(inherited from the catalog — no classification step); enriched by **link-graph centrality**
(ordering + the hubs rail) and **recency** (the updated rail). Explicit owner
categories/portals are an optional later layer; the derived taxonomy is the default.

**Search includes wiki articles.** Extend the Phase-2 hybrid search (dense + FTS, RRF k=60
over `chunks`) with a **wiki leg**: the `wiki_index` summaries are already embedded; add FTS
over revision bodies; RRF-merge so results **blend notes + wiki articles**, each with a
result-type badge (Note / Wiki). The **firewall rides section RLS** automatically (a scoped
viewer's wiki hits exclude out-of-scope sections). Because an article is usually the better
answer to "what do I know about X," **articles rank as the headline result with notes
beneath** — the wiki becomes the *answer layer* over the note substrate, which the **agent**
also retrieves first (high-level + cited) before drilling to facts/notes. *(Search code
reuses `search/service.py`+`repo.py`; the wiki leg has content only once the gated builder
has produced articles → Wave C, with the Search-UI badge + landing in Wave B against
fixtures.)*

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
8. **Source depth (fact-only vs note-derived):** ✅ RESOLVED — **B: chunk-only
   note-derived claims allowed.** The builder sources from cited chunk *text* (not just
   fact statements) via facts (backbone) + `entity_mentions → chunks` (context); claims
   may be fact-backed or chunk-only; both cite a chunk. **Precedence rule: the entity
   graph wins on any fact-vs-note conflict** — a chunk-only claim contradicting a current
   fact is dropped. **Consequence — new required deliverable:** a **grounding gate** (every
   claim entailed by its cited chunk + same-domain + consistent with the current fact set;
   no resurrecting superseded/retracted content) is now mandatory in the builder wave, not
   optional. (§2 `wiki_citations`, §3 steps 3b/4.)

**Must settle WITH the rebuild stream BEFORE the gated work (builder/citations/links):**
*Written up as a hand-off interface spec — `docs/PHASE6_WIKI_GRAPH_CONTRACT.md` (give it
to the rebuild team).*
4. **Citation contract:** the citable unit's frozen shape + the `fact_id` FK ondelete
   policy (RESTRICT vs SET NULL+rebuild-trigger). *Also covers `wiki_links.to_entity_id`
   resolution (mention → article) — entity id stability + merge/split re-point.*
5. **Delta = the `wiki_built` dirty bit (RESOLVED approach):** the rebuild team maintains
   `wiki_built` on **entities** (false on any fact/identity change — create/edit/merge/
   split/retract; the builder sets true). Replaces the change-feed. (`wiki_built` on
   **notes** is graph-independent — Wave A; note edits also dirty mentioned entities.)

**File now as a standalone bug (independent of Phase 6):** the correction-note weight
doc/code discrepancy (ARCHITECTURE "elevated" vs code "normal") — it affects agent
corrections in production today.

## 7. Waves (PROCESS.md: worktrees, per-task + per-wave adversarial review, one PR/wave)

- **Wave 0 — gates (no code):** the **mock gates** (reader ✅ + Talk ✅, both chosen);
  settle decisions #1–#3 (done); open the cross-stream **citation contract + `wiki_built`
  dirty bit** with the rebuild team (#4–#5). Wave 0 unblocks the rest.
- **Wave A — graph-independent spine (parallel-safe now, after #1–#3):** `wiki_articles`
  (incl. `merged_into_id`/`status` for redirects) + `wiki_sections` (incl.
  `parent_section_id` for subsections) + `wiki_revisions` (append-only, full body → diffs)
  + `wiki_index` tables + RLS + isolation tests (against the STABLE `domain_code`/note/chunk
  provenance — the fact-firewall test is deferred to the citation wave); editorial-config-
  as-data (incl. the type guides + writing-style spec); the `wiki_index` embedding path;
  the `wiki_source_exclusions` table shape (note-id rows are stable); **`notes.wiki_built`
  dirty bit** (graph-independent); the wiki ActionSpec **stubs only** (no schedule seed yet). *(Entity-side, small: `entities.image_sha` + entity-view upload — graph-coupled,
  rides the entity layer; the wiki just reads it.)*
- **Wave B — UI (after the mock gates: reader ✅, Talk ✅, landing pending):** the **reader**
  (chosen Wikipedia-style) on fixtures, citation hover-cards, entity-chip/wiki links, the
  **revision diff view**, the **owner-authored** "discuss this article" → correction path +
  revision anchoring; **the owner-only Rebuild / Exclude-source affordances** (§5); **the
  Talk board** (chosen B — threaded topics + Build-log): thread surface, agent
  wiki-editorial tools, thread↔article anchoring (reuse the Phase-4 agent + transcript);
  **the wiki landing** (§5b — search-first + rails + type index, on fixtures) and the
  **Search-UI wiki badge**. Graph-independent (the explain-sources *depth*, builder
  decision-logging, and the live search wiki-leg ride Wave C).
- **Wave C — builder brain (GATED on the rebuild contract #4–#5):** the four wiki actions
  (`wiki_refresh`/`wiki_rebuild`/`wiki_reindex`/`wiki_prune`, §3c) + their schedules;
  `wiki_citations` (hard FK + Postgres firewall CHECK + isolation test) + `wiki_links`;
  **dirty-bit consumption** (mark-and-sweep), index-match triage, cited rewrite with the
  citability predicate + the **grounding gate** + B's chunk sourcing, source-exclusion
  filtering, **entity-driven merge/split enactment (redirects, §3a)**, per-article blurbs,
  the **search wiki-leg** (RRF over `wiki_index`/bodies), the **builder's Talk Build-log
  posts**, and the **wiki-build budget**.

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
