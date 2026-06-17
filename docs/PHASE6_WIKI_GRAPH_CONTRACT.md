# Wiki ↔ fact/entity contract (Phase 6 ⟷ the salient-fact / entity-graph rebuild)

**Audience:** the entity-graph rebuild work-stream. **Purpose:** the Phase 6 wiki
(`docs/PHASE6_WIKI_PLAN.md`) builds its citation, linking, and incremental-build
machinery on top of the fact/entity layer you are reshaping. To build that machinery
**without rework**, the rebuilt model must guarantee the five things below. This is the
interface — the wiki doesn't care how salient facts are represented internally, only
that these hold. Please design them **in**, not as a later bolt-on.

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

## 3. A **domain tag** on the citable unit

Each citable unit must carry its `domain_code` (it does today). The wiki enforces the
firewall **in Postgres**: `citation.domain_code = section.domain_code = fact.domain_code`
via CHECK/trigger. Keep `domain_code` on whatever the citable unit becomes, with the same
ratchet semantics (a fact's domain may be ≥ its note's domain).

## 4. **Stable entity identity** for wiki-to-wiki links

The wiki links article→article by resolving a mentioned **entity** to its article. We need:

- A **stable entity id / canonical key** to store in `wiki_links.to_entity_id`.
- **Merge/split behavior** we can follow: on merge, a re-point signal (today
  `entity.merged_into_id`); on split, a way to re-resolve mentions to the right new
  identity. The wiki re-resolves links on the next build off these signals.
- Same id-stability / migration-map guarantee as §1, for entities.

*Why:* without it, every cross-article link and "what links here" back-link breaks on a
merge/split/rebuild.

## 5. A reliable **fact change-feed** (the delta the nightly builder consumes)

The incremental builder rewrites only articles whose facts changed since the last run.
Today there is **no reliable change-feed**: `app.facts` has only `created_at` (no
`updated_at`), so a naive `created_at >= last_run` watermark **silently misses** these
change classes (all of which alter an article's correct content):

| change class | today's signal | caught by `created_at`? |
|---|---|---|
| new fact | `created_at` | ✅ |
| in-place interval close (`valid_to` set) | UPDATE in place | ❌ |
| in-place refresh (provenance/render) | UPDATE in place | ❌ |
| `pinned` toggle | UPDATE, no timestamp | ❌ |
| `status → retracted` / held | UPDATE | ❌ |
| **entity merge** (`merged_into_id` re-points) | UPDATE on entity | ❌ |
| note deletion / purge (removal) | row gone | ❌ |
| `resolution.changed` re-key | event | partial |

**Ask — pick one (we'll consume either):**
- **(A) `facts.updated_at`** (+ entity `updated_at`), touched on **every** in-place
  mutation in the persistence layer, indexed. The builder watermarks on
  `updated_at >= last_run`. Simplest.
- **(B) fact-mutation events** emitted into `app.events` (the workflow log) — e.g.
  `fact.created` / `fact.changed` / `fact.retracted` / `entity.merged` — carrying the
  changed `(entity_id, domain_code)`. Cleaner and event-native; the builder subscribes.

Either must cover **all** the classes above. Removals (purge) must be observable too (so
the builder can drop now-uncitable claims), e.g. a `fact.removed` event or a tombstone.

*Why:* without a complete change-feed the wiki goes stale silently — an article keeps
asserting "currently works at Acme" after the fact's interval was closed in place.

---

## Acceptance checklist (what "done" means for the wiki)

- [ ] A stable citable id the wiki can hard-FK to (or a documented migration map).
- [ ] A queryable `is_citable` predicate; superseded/historical stay citable; multi-head
      accumulation preserved; `pending_review` policy confirmed.
- [ ] `domain_code` on the citable unit (firewall-enforceable in Postgres).
- [ ] A stable entity id + merge/split re-point signals for link resolution.
- [ ] A complete fact change-feed (`updated_at` **or** events) covering create / in-place
      close / refresh / pin / retract / **merge** / **purge** / re-key, watermark-queryable.

When these land, the wiki's gated wave (citations, links, nightly builder) can start.
