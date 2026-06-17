# Wiki ↔ fact/entity contract (Phase 6 ⟷ the salient-fact / entity-graph rebuild)

**Audience:** the entity-graph rebuild work-stream. **Purpose:** the Phase 6 wiki
(`docs/PHASE6_WIKI_PLAN.md`) builds its citation, linking, and incremental-build
machinery on top of the fact/entity layer you are reshaping. To build that machinery
**without rework**, the rebuilt model must guarantee the items below (§1–§6). This is the
interface — the wiki doesn't care how salient facts are represented internally, only that
these hold. Please design them **in**, not as a later bolt-on. *(§3, §4-mention-domain, §5-
purge, and §6 were added/strengthened after an independent firewall audit found the wiki's
domain firewall is otherwise unenforceable for exactly the health/finance case it protects.)*

This unblocks the wiki's **gated** wave (`wiki_citations`, `wiki_links`, the nightly
builder). The wiki's now-safe shell (articles/sections/index/UI on fixtures) does not
depend on this — only the fact-consuming parts do.

---

## 1. A stable **citable unit** with a stable id

The wiki stores citations as a **hard FK** to a single citable row (per the binding
ARCHITECTURE spec: *"citations are foreign keys to facts/chunks"*). We need:

- **One addressable row per citable claim**, with a stable UUID. It may be `app.facts`
  as today, or a new "salient fact" table/**view** — the wiki only needs a stable
  `(id)` it can FK to.
- **ID stability across a rebuild**, OR a documented re-resolution/migration path. If a
  rebuild re-issues ids, give us either (a) an `old_id → new_id` map we can migrate
  `wiki_citations.fact_id` against, or (b) a stable natural key
  (e.g. `subject_id + entity_key + predicate + qualifier + valid_from`) we can re-resolve
  on. Without one of these, every citation orphans on rebuild.
- **FK-able from another schema object** (so `ON DELETE RESTRICT`/`SET NULL` works). If
  the citable unit is a view, expose the underlying stable id we can constrain to.

*Why:* a citation with no enforced target is the orphan/firewall hole the wiki design
explicitly forbids (app-level integrity is not acceptable — non-negotiable #3).

## 2. A **citability predicate** (what is fit to cite)

The builder must decide, per fact, whether the wiki may cite it. We need a **clear,
queryable predicate**, not a tribal-knowledge derivation over internal columns. Specifically:

- A way to ask: *is this fact citable?* Today that's roughly `status NOT IN ('retracted')`.
  Expose it as a column, a view flag, or documented SQL.
- **Historical/superseded facts MUST remain citable and queryable** (biographical claims —
  "worked at Initech 2016–2019" cites a now-superseded fact). The spec already promises
  *"superseded facts stay queryable for citation integrity"* — keep that.
- **Accumulating predicates** (non-functional relationships, measurements) legitimately
  have **multiple co-equal current facts** — the wiki does not assume a single "active
  head." Don't collapse them.
- Tell us how `pending_review` facts should be treated (our default: not cited until
  adjudicated — confirm).

*Why:* "cite the active head" is wrong for history + accumulation; we need the real rule.

## 3. A **domain tag** + a **same-domain chunk to cite** (derived-chunk citability)

Each citable unit must carry its `domain_code` (it does today). The wiki enforces the
firewall **in Postgres**: `citation.domain_code = section.domain_code = chunk.domain_code`
(`= fact.domain_code` when fact-backed) via CHECK/trigger.

**The audit-critical part:** a fact's domain **ratchets above** its source note/chunk's
(a health reading captured in a `general` note). Today the system mints a per-domain
`source_kind='derived'` chunk and cites *that*, but `search/repo.py` **excludes derived
chunks** (no embedding), so the wiki's mention/semantic sourcing surfaces the *primary*
general chunk — whose `domain_code` then fails the CHECK against a health section. So the
firewall is unsatisfiable for ratcheted (health/finance) sections unless:

- **Every citable unit — fact-backed AND chunk-only (decision B) — has a same-domain
  (derived) chunk to cite.** Guarantee that the derived-chunk machinery covers (a) facts
  that ratcheted, and (b) **chunk-only note-derived claims** in a ratcheted section (today
  derived chunks are minted only on *fact* materialization, so a chunk-only health claim
  may have no same-domain chunk at all). Either extend derived-chunk minting to cover these,
  or expose a sanctioned way for the wiki to mint a derived (domain-scoped) chunk for a
  claim. Without this the health/finance sections — the whole point of the firewall — can't
  cite anything.

## 4. **Stable entity identity** for wiki-to-wiki links

The wiki links article→article by resolving a mentioned **entity** to its article. We need:

- A **stable entity id / canonical key** to store in `wiki_links.to_entity_id`.
- **Merge/split behavior** we can follow: on merge, a re-point signal (today
  `entity.merged_into_id`); on split, a way to re-resolve mentions to the right new
  identity. The wiki re-resolves links on the next build off these signals.
- Same id-stability / migration-map guarantee as §1, for entities.
- **Entity-keyed owner metadata must survive identity changes.** Entities carry owner-set
  metadata the wiki renders — notably a **profile image** (`entities.image_sha`). On
  merge/split/rebuild it must migrate with the entity identity (on merge, keep the
  survivor's image; the owner may re-pick). Don't drop owner metadata when re-issuing ids.
- **The mention index is a wiki SOURCE, not just a link resolver (decision B).** The
  wiki sources article *context* from `entity_mentions` (entity ↔ chunk) — including note
  detail that never became a fact. So the **entity↔chunk mention linkage must be stable /
  re-resolvable** across the rebuild, and mention **re-routing on merge/split must be
  followable** (we re-pull context on the next build). If the rebuild changes how mentions
  attach to entities, give us the same id-map / signal as §1.
- **Mention-as-source DOMAIN (audit).** `entity_mentions.domain_code` is the *note's
  capture domain*, not the fact's. So a Health section's "same-domain mentions" are empty
  when the health facts were captured in general notes — the wiki would either write Health
  from general chunks (a firewall breach) or write nothing. **Pin which domain governs a
  mention used as a section source**, and ensure a same-domain (derived, per §3) chunk
  exists for it — so a domain section sources only from that domain's chunks.

*Why:* without it, every cross-article link, back-link, AND the note-derived prose breaks
on a merge/split/rebuild.

## 5. A `wiki_built` **dirty bit** on entities (the delta the builder consumes)

The builder rebuilds only what changed. Rather than a fragile `created_at` watermark
(which silently misses in-place mutations), the owner chose a **mark-and-sweep dirty bit**:

**Ask:** maintain a boolean **`entities.wiki_built`** (default **false**), and **flip it to
false on ANY change to the entity's facts or identity** — fact create/edit, in-place
`valid_to` close, refresh, `pinned` toggle, `status→retracted`/held, **merge**
(`merged_into_id`), **split**, and `resolution.changed` re-key. The builder selects
`wiki_built = false` entities, rebuilds their articles, and sets `wiki_built = true`. Expose
it queryable + writable by the builder.

This is strictly more robust than a timestamp/event feed: every write path that touches an
entity's facts/identity already has to flip one bit, so **no change class can be missed**.
(The wiki maintains the parallel `notes.wiki_built` itself — that's graph-independent. A
**note edit must also flip `wiki_built=false` on the entities it mentions** — if the
rebuild owns the mention index, please propagate that; otherwise expose the entity↔chunk
mention delta and the wiki will.)

**Purge is a hard deliverable, not "(or signal removal)" (audit).** Note-purge
hard-deletes facts/entities, so a dirty bit on a row that no longer exists cannot be swept —
the article keeps its now-uncited claims forever. **Purge must enqueue a `wiki_rebuild` for
every article that cited the purged note/entity** (the same pattern as a source-exclusion
edit). The wiki provides the "which articles cite note/entity X" query (`wiki_citations`);
the purge path must call the rebuild enqueue (or emit a `note.purged`/`entity.purged` event
the wiki subscribes to). Without it, deletion — a privacy promise — leaves stale prose.

*Why:* without a complete change-feed the wiki goes stale silently — an article keeps
asserting "currently works at Acme" after the fact's interval was closed in place.

## 6. The entity ROW is single-domain RLS — the cross-domain shell must not read it

`app.entities` is hard-RLS on a **single** `domain_code`. A wiki article is cross-domain,
so a general-scoped principal **cannot read a health-domain entity row** — yet the article
needs a title/image/identity. The wiki resolves this by **copying display identity (title,
slug, image) onto the owner-visible `wiki_article` row** and only resolving the entity
anchor under a **system/owner-scoped builder session**. For that to work the rebuild must:

- Keep the **entity anchor resolvable by the system-scoped builder** (the builder reads
  entities under `SYSTEM_CTX`, like the existing pipelines), and keep the
  **entity→article anchor stable** across merge/split/rebuild (per §1/§4 id-stability).
- Confirm that **no wiki read path renders from the entity row under a scoped session** is
  a *wiki* responsibility (it is — §2 of the plan), but the rebuild must not assume the wiki
  can freely read `entities` at render time. (This is why display identity is denormalized
  onto the article row.)

*Why:* otherwise the cross-domain article shell is either broken (no entity row in scope) or
a cross-domain leak (reading a health entity's name/image to a general principal).

---

## Acceptance checklist (what "done" means for the wiki)

- [ ] A stable citable id the wiki can hard-FK to (or a documented migration map).
- [ ] A queryable `is_citable` predicate; superseded/historical stay citable; multi-head
      accumulation preserved; `pending_review` policy confirmed.
- [ ] `domain_code` on the citable unit (firewall-enforceable in Postgres) **and a
      same-domain (derived) chunk to cite for ratcheted facts AND chunk-only claims** (§3).
- [ ] A stable entity id + merge/split re-point signals for link resolution; **entity
      owner-metadata (profile image) migrates** with identity (§4).
- [ ] A stable / re-resolvable **entity↔chunk mention index** with a **pinned
      mention-as-source domain** (§4) — the wiki sources note-derived context from it (B).
- [ ] An `entities.wiki_built` dirty bit flipped false on ANY fact/identity change
      (create/edit/close/refresh/pin/retract/**merge**/**split**/re-key); note edits dirty
      mentioned entities; **purge enqueues a `wiki_rebuild` of citing articles** (§5).
- [ ] The **entity anchor is resolvable by the system-scoped builder** and stable across
      rebuild; the wiki never needs to read `entities` under a scoped session (§6).

When these land, the wiki's gated wave (citations, links, nightly builder) can start.
