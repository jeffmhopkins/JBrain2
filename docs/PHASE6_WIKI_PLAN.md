# JBrain2 — Phase 6 (Wiki) Build Plan

> **Status (in progress).** Research + two builder dry-runs + a three-reviewer independent
> audit done; this is the post-audit v3. Owner decisions settled: article scope
> (cross-domain article; type-guided single-domain sections; hidden out-of-scope sections),
> revision storage (inline `text`, full-body → reconstructable diffs), reader UI
> (Wikipedia-style + a tap citation card), Talk board (**B**, threaded topics), source
> depth (**B**, chunk-cited note-derived claims + grounding gate; **entity graph wins on
> conflict**), linking (wiki→wiki + muted red-link fallback), the **`wiki_built` dirty
> bit** as the delta mechanism, on-demand rebuild + source-exclusion, entity profile
> images, subsections, and the four engine actions (§3b). All three GUI mock gates
> (reader, Talk, landing — landing = search-first + rails) are **closed**. **Open:** only
> the cross-stream **wiki↔graph contract** (`PHASE6_WIKI_GRAPH_CONTRACT.md`, §6 #4/#5) — the
> audit expanded it to cover four firewall realities (entity-row RLS, derived-chunk
> citability, mention-domain, purge). Most of Phase 6 is graph-coupled
> and gated on the rebuild; the parallel-safe slice now is the article/section/revision/
> index shell, editorial config, the `notes.wiki_built` bit, and the read-only UI on fixtures.

The LLM-maintained wiki: notes → facts → entities → **machine-written articles**, every
claim citing a note, corrected only by out-arguing it with a correction note. Plan doc
only — no code lands from this file.

## 0. Framing: the graph-coupling line

A separate work-stream is **rebuilding the salient-fact → entity-graph**, and that rebuild
changes the fact/entity **shape**, not just IDs. So the coupling runs deep:

- **STABLE → buildable now:** notes, chunks, `domain_code` + the RLS firewall, the
  review-inbox / Proposal primitive (incl. the stubbed `wiki-restructure` kind), the
  embed/pgvector/RRF path, subjects/principals, the storage abstraction.
- **GRAPH-COUPLED → gated on the rebuild:** anything that references a fact/entity or
  reasons about citability/supersession/identity — `wiki_citations`, `wiki_links`, the
  builder's sourcing + rewrite, and the firewall CHECKs that join to facts/entities.

**Hard cross-stream dependency:** before the gated work starts, the rebuild stream must
satisfy `PHASE6_WIKI_GRAPH_CONTRACT.md`. The independent audit found the firewall is **not**
enforceable as first drafted, because three existing realities were under-weighted — so the
contract now covers all of: (a) a stable citable unit + id + citability predicate +
`domain_code`; (b) the **entity row is single-domain RLS** (so the cross-domain article
shell can't read it — resolved in §2 by storing display identity on the article row); (c)
**facts ratchet domain above their source chunk** and cite minted *derived* chunks that
search excludes (derived-chunk citability must extend to chunk-only claims); (d)
**mention domain = the note's capture domain**, not the fact's (a section's source domain
must be pinned); (e) note-**purge hard-deletes**, so purge must dirty + rebuild surviving
articles; and (f) the `wiki_built` dirty bit on entities.

## 1. What already exists (reuse, don't rebuild) — cited

- **Workflow engine** (Phase 5): the wiki actions are **in-code ActionSpecs** (no
  `app.actions` row — mirror `PURGE_ACTION` / the reconcilers; `registry.py`, `scheduler.py`).
  Pipelines + schedules + `manual=true` triggers are seeded by migration **with the
  builder (Wave C)**, not before — a no-op scheduled action would log empty nightly `runs`.
- **Embeddings + retrieval** (Phase 2): TEI `bge-small-en-v1.5` 384-dim, `chunks` pgvector
  HNSW + FTS, hybrid RRF k=60 (`search/service.py`, `search/repo.py`; vector(384)+HNSW per
  migration 0003). The wiki index + search wiki-leg reuse this path. *(Note:* `search/repo.py`
  excludes `source_kind='derived'` chunks — relevant to derived-chunk citability, §2/§3.)*
- **RLS + isolation-test pattern**: `ENABLE/FORCE RLS` + `has_domain_scope(domain_code)`
  policy + an isolation test per new table (mirror `test_lists_rls.py`; for revisions, the
  section-join-EXISTS RLS pattern of `0022_lists.py`).
- **LLM adapter**: `router.complete(task="wiki.rewrite", json_schema=…)` — never a SDK.
- **Storage abstraction**: blob-store entity images (and optionally revision bodies) by sha256.
- **Review inbox / Proposals**: `wiki-restructure` kind exists (`0018_proposals.py`).
- **Frontend**: React+Vite+TS PWA. Reuse `FactCitation` (`analysis/bits.tsx`), `MatchBadge`
  (`screens/SearchScreen.tsx`), the `EntityScreen` read-only paradigm, the shared `Sheet`
  shell for the citation card + discuss flow. The **Wiki launcher tile is stubbed**
  (`Launcher.tsx:53`, `phase:"P6"`).

## 2. Storage (new tables) — cross-domain articles, type-guided single-domain sections

Citations are **hard FKs** to facts/chunks (ARCHITECTURE.md §Wiki). All RLS-scoped,
isolation-tested. **Article model:** one CROSS-DOMAIN article per subject/entity, body in
type-guided **single-domain SECTIONS**; the section is the firewall/RLS/revision/index
unit; section existence (not just content) is hidden from out-of-scope principals; only the
owner sees every section. Citations render Wikipedia-style: a tap **citation card** + a
numbered References list. (Chosen reader: `docs/mocks/wiki-reader-chosen-wikipedia.html`;
worked example: `docs/mocks/wiki-reader-example-priya.html`.)

**Buildable now (graph-independent table shape — no FK into facts):**
- **`app.wiki_articles`** (cross-domain): `id`, `entity_ref` (the subject/entity anchor —
  used only by the builder under a **system-scoped** session, never read to render the
  shell), `title`, `slug`, `image_sha` (the profile image — **copied onto the article row**
  so the shell never reads the single-domain-RLS entity row), `lead_summary` (the 1–2
  sentence blurb for the landing + search) + `lead_embedding vector(384)`, `status`
  (active|merged|archived), `merged_into_id` (reversible redirect), `created_at`,
  `updated_at`. **Owner-visible**; a scoped principal sees an article iff ≥1 of its sections
  is in scope. *Display identity lives here, decoupling the shell from entity RLS (audit
  blocker 1).*
- **`app.wiki_sections`** (the firewall unit): `id`, `article_id` (FK ON DELETE CASCADE),
  `domain_code` (FK, NOT NULL), `parent_section_id` (self-FK, nullable — **subsections**),
  `current_revision_id` (FK→revisions ON DELETE SET NULL), `seq`. **RLS
  `has_domain_scope(domain_code)` governs content AND existence.** A **trigger** (not a plain
  column CHECK — it must compare against another row) forces a non-root section's
  `domain_code` to equal its **root's** on insert/update (validated up the `parent_section_id`
  chain), so the whole subtree shares one domain and hides together; no cross-domain nesting,
  enforced in Postgres.
- **`app.wiki_revisions`** (append-only, per section): `id`, `section_id` (FK ON DELETE
  CASCADE), `seq`, `run_id` (FK→`app.runs`), `body` (inline `text`, markdown), `summary`,
  `body_tsv` (generated tsvector + GIN, for the search wiki-leg), `created_at`. Immutable;
  full body kept → **any diff (incl. each build's) is reconstructable**, no extra storage.
  **RLS rides the parent section** via a section-join EXISTS policy (revisions carry no own
  `domain_code`) — so FTS over bodies can't leak out-of-scope revisions.
- **`app.wiki_index`** (per section): `id`, `section_id` (FK), `domain_code`, `summary`,
  `summary_embedding vector(384)`, `embedding_model`, HNSW. The domain-scoped match target;
  all ANN queries run inside the RLS-scoped session.
- **Editorial config as data**: style guide, citation-density floor, the **notability gate**
  (§6), and the per-entity-type **wiki guides** (sections/style/requirements; starter set
  `docs/WIKI_TYPE_GUIDES.md`). *(Split/merge thresholds are vestigial under the
  entity-driven model — omitted from v1.)*

**Gated on the rebuild (FK into the frozen fact shape):**
- **`app.wiki_citations`**: `id`, `revision_id` (FK ON DELETE CASCADE), `fact_id`
  (nullable, **hard FK → app.facts(id) ON DELETE SET NULL** — a *fact-backed* claim; null
  for a *chunk-only* note-derived claim. **SET NULL, NOT RESTRICT** — RESTRICT would abort
  the privacy-purge transaction, which hard-deletes facts; purge enqueues a rebuild instead,
  §3b), `chunk_id` (hard FK → `chunks`, NOT NULL ON DELETE CASCADE — every claim cites a
  chunk; honors the note-deletion purge), `note_id` (derivable from `chunk_id` — **stored
  denormalized for the References render**, FK ON DELETE CASCADE), `domain_code`. **Firewall
  in Postgres:** CHECK/trigger `citation.domain_code = section.domain_code = chunk.domain_code`
  (and `= facts.domain_code` when fact-backed), **and `citation.note_id = chunk.note_id`**
  (so the denormalized note can't diverge from the chunk's note and the two CASCADEs can't
  fire on different purges). **Purge vs. supersession:** on **purge**, `chunk_id`/`note_id`
  CASCADE delete the whole citation row, then the article is rebuilt (§3b) to regenerate
  surviving prose — `fact_id ON DELETE SET NULL` only applies to **non-purge** supersession
  (the fact is replaced but its note survives, so the citation stays valid). **Derived-chunk
  citability:** the cited `chunk_id` must be the **same-domain (derived) chunk** when a fact
  ratcheted above its source note's domain; for chunk-only claims in a ratcheted section, the
  builder mints a derived chunk (contract §3) so a same-domain chunk always exists to cite.
  Isolation test: a scoped session can neither create nor read a cross-domain citation, nor
  observe an out-of-scope section.
- **`app.wiki_links`** (wiki↔wiki + back-links): `id`, `from_section_id` (FK ON DELETE
  CASCADE), `to_entity_id` (**a SOFT reference — no FK**, because `entities` is single-domain
  RLS and a cross-domain link can't carry an FK readable by a scoped back-link query;
  resolved system-scoped at build, like `entity_ref`), `to_article_id` (FK, nullable),
  `anchor`, `domain_code`. Powers article→article links, **"what links here"**, and the
  notability signal. **All counts (back-links, centrality, hubs) are computed inside the
  RLS-scoped session** (post-RLS), so a scoped principal's totals equal their visible links —
  never the global tally. Back-links RLS-filtered by `from_section.domain_code`.
- **`app.wiki_source_exclusions`** (owner suppression): `id`, `note_id` (nullable, FK to
  `notes` — **stable, buildable in Wave A**) **or** `fact_id` (nullable, FK — **gated; the
  FK is added in Wave C with the frozen fact shape**) — exactly one, `scope` (`global` | an
  `article_id`), `reason`, `created_at`, `domain_code`. Owner-only RLS; audited. **≠ deletion**
  (still searchable) and **≠ retraction** (still true). An insert/delete enqueues a
  `wiki_rebuild` of affected articles. *(The note-id half rides Wave A; the fact-id FK rides
  Wave C — like the dirty bit's notes/entities split.)*

**Entity profile image** (owner metadata, not a claim): set in the **entity view**,
blob-stored (`entities.image_sha`), and **copied to `wiki_articles.image_sha`** at build so
the article shell renders it without reading the RLS-hidden entity row. No citation. The
entity-side column is graph-coupled (contract §4 — survives merge/split/rebuild).

## 3. The builder (GRAPH-COUPLED — gated)

The builder runs as workflow-engine actions (§3b). The two build entry points share the
pipeline below; both honor the **source-exclusion list** (§2) and the firewall.
- **`wiki_refresh`** — incremental, dirty-bit driven (the workhorse).
- **`wiki_rebuild`** — full re-derive `{article_id|"all"}`, non-delta, `"all"` chunked.

Pipeline (ARCHITECTURE.md §Wiki):

1. **Delta via the `wiki_built` dirty bit (mark-and-sweep).** A boolean on **notes** and
   **entities**, false on create/edit (and on fact change / merge / split / retract), set
   true once incorporated. The builder targets `wiki_built=false` rows and marks them true.
   It cannot miss an in-place mutation (every write path flips the bit). A note edit also
   **dirties the entities it mentions** (so B's chunk-only context is picked up with no fact
   change). **Purge** is handled out-of-band: purge enqueues `wiki_rebuild` for affected
   articles (§3b), because a hard-deleted row can't carry a dirty bit.
2. **Index match** — embed the dirty cluster, RRF against `wiki_index` → candidate articles.
3. **Triage** — one cheap LLM call per cluster → update | create | split | merge | ignore.
   `create` is gated by the **notability gate**; a sub-threshold entity stays
   link-target-only.
4. **Type + guide** — article type = the entity's kind → load that type's wiki guide.
5. **Source-finding (decision B), per affected section's DOMAIN.** *Backbone* = the
   entity's citable facts (`entity_id`/`object_entity_id`), each with its same-domain (derived
   if ratcheted) chunk. *Context* = chunks **of the section's domain** that mention the
   entity — sourced via the **fact/section domain, not the note-capture-domain mention set**:
   for a Health section, use health-domain (derived) chunks, never the
   general capture chunk. The builder writes prose from chunk text; claims are fact-backed
   or chunk-only; **every claim cites a same-domain chunk** (minting a derived chunk for a
   chunk-only ratcheted claim if needed). Then **filter the source-exclusion list**.
6. **Cited rewrite + grounding gate** — `router.complete("wiki.rewrite", json_schema=…)`
   per the type guide; **resolve mentions to wiki links** (article if it exists, else a
   muted red-link to the entity page). **Citation rule:** every claim cites a chunk (and a
   fact when fact-backed) that is **non-retracted and same-domain** — NOT "the active head";
   superseded/historical facts stay citable; accumulating predicates have multiple co-equal
   current facts. **Grounding gate (required):** a verifier asserts **each cited clause is
   entailed by its cited chunk** (clause-granular, matching the clause-level citation rule),
   same-domain, and consistent with the entity's current fact set; **the entity graph wins
   on conflict** — a chunk-only claim contradicting a current fact is dropped. Fail-closed.
7. **Merge/split** — follows the ENTITY graph (§3a); the builder enacts redirects/
   re-partition for dirtied entities, logs the Talk Build-log, re-resolves links/citations.
8. **Re-embed** changed section summaries into `wiki_index`; **emit the per-article
   `lead_summary` (+ embedding)** for the landing/search; **mark built notes/entities
   `wiki_built=true`** (close the loop).

## 3a. Taxonomy & merge/split (article identity = entity identity)

**Taxonomy is inherited, not invented.** One article per notable entity; type = entity kind
→ wiki guide → sections. Derived signals enrich it (link centrality, recency); no manual
classification. Categories/portals are an optional later layer.

**Merge** (entity A → B): B absorbs A's re-pointed facts/sources; **A's article becomes a
reversible redirect** to B (`status=merged`, `merged_into_id`, title→alias; never deleted).
Both dirtied → rebuild B; Build-log "merged A into B"; links/citations re-resolve. **Redirect
firewall:** a redirect to B is followable (and B's title/slug revealed) **only if B has ≥1
in-scope section for the principal** — otherwise the redirect is opaque/404 to that scope, so
a merge can't leak the existence/name of an all-out-of-scope target.
**Split** (X → X+Y): partition by the new identities; rewrite X; create Y if notable;
re-resolve. Reversible.

**Approval:** the **entity** merge/split is the single owner-approved decision (review
inbox); the article restructure is a downstream, logged, reversible build effect. *This is a
reinterpretation of ARCHITECTURE.md "split/merge approvals via the review inbox" (which the
audit flags for explicit owner sign-off, §6 #10).* A builder-detected candidate surfaces as
a `wiki-restructure` proposal that **routes to the entity-level** decision (no wiki/graph
drift). **Deferred:** purely-editorial length splits.

## 3b. Actions & workflows (the engine map)

Four **in-code ActionSpecs** (no `app.actions` row); pipelines + schedules + `manual=true`
triggers seeded by migration (Wave C); every run a `runs` row in the Ops Automations catalog.

| Action | Does | Triggers | Class |
|---|---|---|---|
| **`wiki_refresh`** | Incremental, dirty-bit driven: build/update dirty entities' articles, enact merge/split redirects, re-embed, emit blurbs, post Build-log, mark clean. **Self-reconciling** (a dropped enqueue self-heals next run). | nightly schedule + Ops manual + (opt) entity-change event | expensive · mutating · budgeted |
| **`wiki_rebuild`** | Full re-derive `{article\|"all"}`; ignores dirty; `"all"` chunked. | Ops manual; enqueued by exclusion edits, prompt/guide bumps, **and note-purge** (rebuild articles citing the purged note/entity) | expensive · mutating · budgeted |
| **`wiki_reindex`** | Re-embed all `wiki_index` summaries after an embedding-model swap (mirrors `sync_predicates`). | Ops manual + on model change | standard · mutating(index) |
| **`wiki_prune`** | Archive/redirect **orphaned** articles (entity purged / below notability); GC (mirrors `purge_deleted_artifacts`). | nightly (after refresh) + Ops manual | cheap · mutating |

**Budget:** a dedicated **wiki-build token budget** (mirrors the integration budget;
SEPARATE from the self-improvement/eval budget). `wiki_rebuild("all")` chunks. **Schedule:**
~03:30 UTC, after the 02:00 graph sweeps + 03:00 eval.

## 4. Editorial discussion board ("Talk") + the correction loop

A persistent, article-anchored **Talk page** (chosen direction **B**, threaded topics +
Build-log — `docs/mocks/wiki-talk-b-topics.html`). **Two voices:** the batch builder posts
decision summaries (split/merge/dropped/excluded); the interactive **Phase-4 agent**
converses, explaining the article from the build run + citations + guide. **The wiki stays
machine-written** — Talk is the conversational front-end over the sanctioned levers
(correction note, source exclusion, rebuild, split/merge proposal). Reuse the agent loop /
Proposals / transcript; a thread = an agent session anchored to an `article_id`. New =
wiki-editorial tools (`explain_article`/`get_sources`, `file_correction`,
`add_source_exclusion`, `request_rebuild`, `propose_split_merge`). Owner-only; the agent's
reads are firewalled.

**Correction-note path — owner-authored, elevated-weight, revision-anchored.** The existing
`propose_correction` makes an *agent*-authored NORMAL-weight note — the wrong path. This
needs an owner-authored note path + a revision-anchoring column. **Prerequisite:** the
**elevated-weight extraction path does not exist in code today** (ARCHITECTURE.md and
`notes.py` claim it; `proposaltools.py` is NORMAL; no "elevated" in the weight model) — so
the correction loop's exit criterion ("out-argue the wiki") is blocked until it's built.
**This is a named Wave-0 prerequisite** (not a floating bug): confirm/build the
owner-correction elevated-weight path before the correction loop ships.

## 5. Read-only wiki UI — reader (mock gate ✅)

Mock gate ✅ — owner chose **A refined to a Wikipedia-style reader**
(`docs/mocks/wiki-reader-chosen-wikipedia.html`): infobox (with the profile image), prose
lead, type-guided **nested sections (H2/H3/H4)**, bulleted lists + tables (writing-style
spec), and **Wikipedia-style citations — a tap `[n]` opens a citation card** (source note ·
date · domain · snippet) **and** the numbered References list. The card renders **only the
cited same-domain (derived) chunk's text** and never dereferences the source note across a
firewall (a ratcheted fact's capture note can be a different domain). Full-screen read-only, amber
read-only tint, **tokens-only** (re-skinned to `tokens.css`; domains: medical=rose,
finance=violet, general=steel), reuse the shared `Sheet` shell. wiki→wiki links (article or
muted red-link); a "what links here" affordance; "discuss this article" → the Talk/correction
path. Built against **fixture data** (graph-independent). **DoD:** fixtures for default /
empty / long-article / error / offline.

**Owner-only editorial affordances** (curation, not text edits): **Rebuild** and **Exclude
this source** (on an owner "⋯" / per-reference action) — enact in the gated builder wave.

## 5b. Wiki landing + search

**Landing — a living, search-first home** (mock gate ✅ — chosen **A**,
`docs/mocks/wiki-landing-a-search-rails.html`). Rails, each entry = title + the per-article
`lead_summary` blurb:
- **Search box** (the article-aware search below).
- **Recently updated** — from the last build (Build-log / `runs`).
- **Most connected (hubs)** — top inbound `wiki_links`, **computed post-RLS** (a scoped
  principal's counts = their visible links only; isolation test required).
- **Type-grouped index** — People · Organizations · Places · … collapsible, A–Z within.

**Taxonomy is derived** (entity type + centrality + recency), never hand-maintained.

**Search includes wiki articles.** Extend the Phase-2 hybrid RRF with a **wiki leg**: dense
over `wiki_index.summary_embedding` + FTS over `wiki_revisions.body_tsv`, RRF-merged with
note results, each with a type badge (Note / Wiki). **All wiki-leg queries (ANN + FTS) run
inside the RLS-scoped session** (revisions via the section-EXISTS policy), so out-of-scope
sections never rank or leak via ordering — isolation test required. An article usually
out-answers a raw passage, so articles rank as the headline result with notes beneath — the
wiki becomes the *answer layer* the agent also retrieves first.

## 6. Open decisions

**Settled (recorded in this doc):**
1. Article scope — cross-domain article, type-guided single-domain sections, hidden
   out-of-scope sections.
2. Revision storage — inline `text` (full body → diffs).
3. Reader UI — Wikipedia-style + tap citation card.
6. Notability gate — *default:* entity with ≥3 cited facts OR ≥2 notes, on article-worthy
   types; tunable in editorial config.
7. Link fallback — wiki→wiki + muted red-link entity-page fallback.
8. Source depth — **B** (chunk-cited note-derived claims + grounding gate; entity graph
   wins on conflict).

**Open — owner / cross-stream:**
4. **Citation contract** (`PHASE6_WIKI_GRAPH_CONTRACT.md` §1-§4): the citable unit's frozen
   shape + `fact_id` SET NULL + **derived-chunk citability for chunk-only claims** + entity
   id stability for `wiki_links.to_entity_id`.
5. **Dirty bit + entity visibility + purge** (contract §4-§6): `entities.wiki_built`
   maintenance; the **entity row's single-domain RLS vs the cross-domain shell** (resolved
   here by §2's article-row display identity — confirm with the rebuild team);
   **mention-as-source domain**; **purge dirties + rebuilds** surviving articles.
9. **Landing mock gate** — ✅ RESOLVED: owner chose **A — search-first + rails**
   (`docs/mocks/wiki-landing-a-search-rails.html`): search + Recently-updated + Most-connected
   hubs + a Browse-by-type index with blurbs (taxonomy derived: type + centrality + recency).
   C's graph view may return as an optional secondary tab. (All three GUI mock gates —
   reader, Talk, landing — are now closed.)
10. **Split/merge approval reinterpretation** — confirm the article restructure as a
    downstream build effect (not a second review-inbox approval) is acceptable vs the
    binding ARCHITECTURE wording.

**Prerequisite with a build home (not a floating bug):** the owner-authored
**elevated-weight correction** extraction path + note→revision anchoring does not exist in
code; the correction loop depends on it. **Built in Wave A+** (graph-independent backend),
consumed by Wave B2b.

## 7. Waves (PROCESS.md: worktrees, per-task + per-wave adversarial review, one PR/wave)

- **Wave 0 — gates (no code/PR):** the **landing mock gate** (#9); the cross-stream
  **contract** hand-off (#4/#5); the **ROADMAP + ARCHITECTURE edit** (move Loops 2–4 + the
  hygiene sweeps out of Phase 6 into named follow-ons — see Out-of-scope; and reconcile
  ARCHITECTURE.md §Wiki, which still asserts split/merge *thresholds*, review-inbox approval
  of the *article* restructure, and an elevated-weight path that doesn't exist — all
  superseded by this plan); confirm #10.
- **Wave A — graph-independent spine (parallel-safe now):** `wiki_articles` (display
  identity incl. `image_sha`/`lead_summary`, `merged_into_id`/`status`) + `wiki_sections`
  (incl. `parent_section_id` + the domain-inheritance CHECK) + `wiki_revisions` (full body +
  `body_tsv` + section-EXISTS RLS) + `wiki_index` tables + RLS + isolation tests (against the
  STABLE `domain_code`/note/chunk provenance; the fact-firewall test is deferred to Wave C);
  editorial-config-as-data (+ type guides); the `wiki_index` embedding path; the
  `wiki_source_exclusions` table shape (note-id rows); **`notes.wiki_built`** (graph-
  independent). *(Entity-side, gated/small: `entities.image_sha` + entity-view upload — rides
  the entity layer; the `entities.wiki_built` bit + mention→entity dirtying are the rebuild
  team's, contract §5.)*
- **Wave A+ — correction-note backend (the elevated-weight prerequisite):** **build** (not
  just scope) the owner-authored, **elevated-weight** extraction path + the note→revision
  anchoring column. Graph-independent backend; B2b's correction loop strictly depends on it,
  so it gets its own build home here rather than falling through the B2 boundary.
- **Wave B1 — read-only reader (after the reader mock ✅):** the Wikipedia-style reader on
  fixtures, the **tap citation card**, nested sections, lists/tables, the profile image, the
  **revision diff view**, wiki→wiki links, "what links here". Pure read-only; one PR.
- **Wave B2a — landing + search UI (after the landing mock):** the wiki **landing**
  (search-first + rails + index) on fixtures + the Search-UI wiki badge. Read-mostly UI; one PR.
- **Wave B2b — Talk + correction machinery (scope-touching):** the **Talk board** (B) —
  thread surface, agent wiki-editorial tools, thread↔article anchoring; the owner-authored
  correction-note path (consumes Wave A+) + revision anchoring; the owner-only Rebuild/Exclude
  affordances. Wave-level red-team for the agent's firewalled reads; its own PR.
- **Wave C — builder brain (GATED on the contract):** the four actions (§3b) + their
  schedules + the wiki-build budget; `wiki_citations` (hard FK + Postgres firewall CHECK +
  isolation test) + `wiki_links`; dirty-bit consumption; index-match triage; cited rewrite +
  **grounding gate** + B's derived-chunk sourcing; source-exclusion filtering; entity-driven
  merge/split enactment (redirects); per-article blurbs; the **search wiki-leg** (RRF over
  `wiki_index`/`body_tsv`, post-RLS) — *separable; may follow as Wave C2 if Wave C is too
  large*; the builder's Build-log posts; **purge → rebuild** wiring. **Blocking DoD:**
  *chunk-only derived-chunk minting* — without it a ratcheted health/finance section has no
  citable chunk and renders empty, so the entire firewalled-domain wiki depends on it; it is
  a named exit gate for this wave, not a sub-bullet.

**Out of scope (named follow-ons, each its own plan — the ROADMAP edit relocates these here):**
the **not-yet-built hygiene sweeps** the ROADMAP lists under Phase 6 — **entity hygiene,
summary re-embedding, tag consolidation** (distinct from `wiki_reindex`, which only re-embeds
wiki summaries) — engine-action work on the Phase-5 pattern, deferred, not silently dropped.
*(The self-improvement Loops 2–4 once listed here — skill learning, durable-knowledge +
predicate-canon, prompt/tool self-edit — and their eval/promotion harness were removed.)*

**Deferred wiki sub-features (built half-way, explicitly tracked, not implied-working):**
- **Entity profile images** — SHIPPED. `entities.image_sha` (migration 0052) is set by the owner
  via the entity view (PUT `/api/entities/{id}/image`, multipart, magic-byte sniffed — the
  Content-Type is not trusted), stored content-addressed in the blob store, and copied onto
  `wiki_articles.image_sha` both directly on upload (cheap, no LLM rebuild) and at every build
  (`_ensure_article`). The reader emits `infobox.photo` + an `image_url` (GET `/api/wiki/{id}/image`)
  and the Infobox renders the `<img>`; the entity view shows + uploads the photo. The image is
  owner metadata, not a claim — prose stays machine-written (#7).
- **Inline wiki→wiki links in prose** — SHIPPED. The builder (`_write_section` → `_linkify`)
  weaves `[anchor](wiki:<slug>)` / `[anchor](redlink)` markers into the persisted section body for
  each relationship fact's object entity (live vs red resolved at build time from the target's
  active article), so the reader's live/red-link rendering path is now exercised end-to-end. An
  anchor the grounded prose phrases differently is left unlinked; the `wiki_links` row still
  records the connection. Article→article navigation on tap remains a later reader wave.
- **Threaded Talk board** — Wave **T1 shipped**: the persistent board (`wiki_talk_topics`/
  `wiki_talk_posts`, owner-only RLS, migration 0053), the **Builder** voice (a Build-log decision
  summary posted per rebuild), owner topics/replies/resolve (`GET/POST/PATCH /wiki/{id}/talk…`),
  and the B-topics reader screen (`TalkScreen`) reachable via the reader's Discussion affordance.
  **Wave T2 shipped:** the live **Editor** (agent) reply — an owner reply draws an `AgentLoop.run()`
  turn (dedicated Editor system prompt + the wiki tools, owner full-read, `after_post_id` idempotency
  guard) that explains sourcing and enacts via the levers, posted as an `editor` post with an outcome
  chip (`POST /wiki/{id}/talk/topics/{tid}/editor`). See `docs/TALK_BOARD_PLAN.md`. Deferred follow-up:
  fold the reader's DiscussSheet quick-fix into the in-topic flow.

## 8. Non-negotiables (CLAUDE.md) + exit

Adapter-only LLM; storage abstraction; **firewalls enforced in Postgres** (the citation +
subsection + revision-EXISTS CHECKs/policies, post-RLS counts — not app code) + an isolation
test per new table; machine-written wiki, humans correct via correction notes only (#7);
tests-with-code 80% / security-100%; Conventional Commits + per-wave PR + CI green;
`dev-setup.sh` current.

**Exit (ROADMAP):** a day of notes updates only the affected articles overnight, every claim
cites a note, corrections happen by out-arguing the wiki. **Acceptance test (incrementality):**
N dirty entities → exactly their articles rewritten, others byte-identical (a named Wave-C
DoD test, not an implied property).
