# Wiki landing — mock gate (Phase 6, §5b; open decision #9)

Three interactive directions for the **wiki landing page** — the entry point behind the
Wiki launcher tile (the wiki's *third* new GUI surface, after the reader and Talk). Pick
one; the chosen mock becomes the binding spec. All honor the plan: **taxonomy is derived,
not hand-maintained** (entity type + link-graph centrality + recency), and the landing is
read-only (amber tint), tokens-only (real DESIGN palette), phone-framed, dark-first + theme
toggle, outline icons, with **1–2 sentence blurbs** = each article's stored `lead_summary`.
Article type discs use the DESIGN entity-type accents (Person=steel, Org=violet, Place=green).

| File | Direction | Shape | Best when |
|---|---|---|---|
| `wiki-landing-a-search-rails.html` | **Search-first + rails** (the plan's recommendation). A prominent search box, a "Recently updated" horizontal rail, a "Most connected (hubs)" list (inbound-link counts), then a collapsible **Browse by type** index with blurbs. | A living home. | The wiki is used daily; you want pulse (recent) + entry points (hubs) + the index, in one scroll. |
| `wiki-landing-b-catalog.html` | **Catalog / portal.** Search + type filter chips, then the **type-grouped index dominates** — section per type with counts, A–Z, every article a disc + blurb. | A complete directory. | You think of the wiki as a browsable encyclopedia and want the full catalog front-and-center. |
| `wiki-landing-c-graph.html` | **Graph / map.** Search + a visual **link-graph** of the most-connected articles (type-colored nodes, `wiki_links` edges) — tap a node → a card → open; a "Recently updated" feed beneath. | You think in connections. | Exploration by relationship; the hub structure *is* the navigation. Visually distinctive, can get busy at scale. |

## Trade-offs

- **A** balances discovery (search), liveness (recent), importance (hubs), and completeness
  (index) — the most generally useful, and everything on it is derived for free. Densest.
- **B** is the cleanest to scan when you know roughly what you're looking for, but it's
  static — no "what changed" or "what matters", and it grows unboundedly with the corpus.
- **C** is the most novel and best conveys the knowledge *network*, but a node-graph is
  noisy past ~30 nodes and is weaker for "find a specific article" (search carries that).
  Strong as a **secondary** view even if not chosen as the primary.

## Decision

**Chosen: A — search-first + rails** (`wiki-landing-a-search-rails.html`): a prominent
search box, a Recently-updated rail, a Most-connected (hubs) list, and a collapsible
Browse-by-type index with `lead_summary` blurbs. The living-home direction; everything on
it is derived for free (recency + centrality + type). C's graph may return later as an
optional secondary view/tab. B/C retained as the record. The choice + rationale land in
`DESIGN.md` when the landing UI is built (Wave B2a). Search is the article-aware hybrid
search (§5b). DoD: fixtures for empty (no articles yet) / few-articles / many-articles /
offline. **This closes the last Phase-6 mock gate.**
