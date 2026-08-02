# Grokipedia Tool — Research Dossier

> **Status:** Living · **Last verified:** 2026-08-02

Research dossier for a **Grokipedia search/traversal tool** for the `jerv` agent
persona. Goal: let the agent get information on any subject, news, or current
event *fast* — search Grokipedia, pull a table-of-contents / tree of an article,
drill into specific sections rather than dumping the whole article into context,
and **extract citations so the agent can follow them to primary sources**.

**Binding constraint (from the requester):** no xAI/Grok API key. All access is
via the **open internet** — public HTTP, public search, sitemaps, scraping of the
public `grokipedia.com` site. This dossier confirms that constraint is not just
satisfiable but comfortable: the cleanest access paths never touch the paid
xAI API surface.

Produced by three parallel research sweeps (2026-08-01): (A) Grokipedia's public
surface, verified by direct `curl`; (B) prior art on agent-facing encyclopedia
tools; (C) how the tool fits JBrain2's existing agent/tool architecture.
**Nothing is built** — this is the menu and the recommendation that a Proposed
plan would draw on.

---

## 1. Executive summary — what to build

- **Grokipedia is an easy target.** Article pages are **fully server-side
  rendered** (the entire body, TOC, and all citations are in the raw HTML), *and*
  the site's own frontend calls unauthenticated same-origin JSON endpoints that
  return clean Markdown + a structured citation array. **No xAI key, and almost
  certainly no headless browser, is needed.** The only friction is Cloudflare,
  which 403s default library User-Agents but serves a normal browser UA.
- **Citations are first-class.** Every article exposes its sources as discrete,
  structured objects (`{id, title, description, url, favicon}` in JSON; a
  numbered `<li id="ref-N"><a href>` reference list in HTML). The requester's #1
  goal — follow citations to primary sources — needs no heuristic footnote
  parsing.
- **Copy the proven shape.** The reference `wikipedia-mcp` server and the
  community Grokipedia clients converge on the same small tool set. Anthropic's
  own tool-design guidance says the same: few high-leverage tools, human-readable
  drill-in keys, token-bounded responses.
- **Recommended tool set (5 tools), namespaced `grokipedia_*`:**
  `grokipedia_search` → `grokipedia_outline` → `grokipedia_section` → `grokipedia_citations` →
  `grokipedia_related`, with a rarely-used `grokipedia_article` full-dump escape hatch. This
  is the TOC-then-section-then-citations loop the requester described.
- **It fits JBrain2 cleanly.** It is a `web`-permission tool set for `jerv`
  (which already fetches arbitrary web content directly, no egress Proposal),
  built as `.tool` sidecars + `httpx` client under `web/`, reusing
  `guard_public_host`, `trafilatura`, the cross-turn tool-artifact cache, and
  `ToolOutput`/`WebSource` citation surfacing. **Goal: zero new runtime deps.**
- **One real decision to make** (see §6): the cleanest data (structured JSON) is
  behind endpoints that `robots.txt` disallows, while the fully robots-compliant
  path (SSR HTML + `site:` search) is slightly messier and lower-recall. Default
  recommendation: robots-compliant path as the shipping default, JSON endpoints
  as an operator-gated fast path.

---

## 2. Grokipedia's public surface (verified 2026-08-01 by direct `curl`)

### URLs & corpus
- **Canonical article URL:** `https://grokipedia.com/page/<slug>` — one stable URL
  per article, emitted as `<link rel="canonical">` / `og:url`. Real examples:
  `/page/Elon_Musk`, `/page/Slug_(publishing)`, `/page/Grokipedia_index`.
- **Slug formation:** Wikipedia-style — title with spaces → underscores, case and
  punctuation preserved, special characters entity/URL-encoded. Internal wiki
  links inside article Markdown use `[Topic](/page/Topic_Slug)`.
- **Corpus size:** `/api/stats` reports `totalPages: 6,092,140` (~194 GB index),
  matching the sitemap math (235 × 25,000 ≈ 5.88M).

### Rendering — SSR, no headless browser
- Raw HTML for `/page/Elon_Musk` is **1.76 MB** and contains the entire article
  body, every heading, all 832 references with external links, meta/OG tags, and
  image URLs — **no JS execution required.** No `__NEXT_DATA__`, no `#__next`, no
  external script bundles. A strict CSP with `connect-src 'self'` means the
  frontend only ever calls same-origin `grokipedia.com/api/...` endpoints.

### Same-origin JSON endpoints (the frontend's own XHR targets)
All returned JSON, unauthenticated, with a browser UA. **All are under `/api/`,
which `robots.txt` disallows** (see §6):

| Endpoint | Purpose |
|---|---|
| `GET /api/typeahead?query=<q>&limit=<n>&offset=<n>` | **The real search** (autocomplete). Paginated. |
| `GET /api/page-preview?slug=<slug>` | Full article as Markdown + `citations[]` + metadata + images + stats. |
| `GET /api/stats` | Corpus stats (`totalPages`, index size). |
| `GET /api/list-edit-requests-by-slug?slug=<slug>` | Community pending edit-request queue for a page. |
| `GET /api/search?...` | **Does NOT exist** — falls through to the SPA shell. Prior-art repos that claim it are wrong; use `/api/typeahead`. |

`/api/typeahead` result objects: `{slug, title, snippet, relevanceScore,
viewCount, creationSource, visibility, snippetVariants, titleHighlights,
snippetHighlights}`. `/api/page-preview` `page` object: `content` (extended
Markdown, with an HTML-comment-delimited infobox block), `citations[]`
(`{id, title, description, url, favicon}`), `metadata`
(`{lastModified (epoch), contentLength, version, categories[], lastEditor,
language, isRedirect, ...}`), `stats` (`{totalViews, recentViews, qualityScore,
...}`), `images[]` (`{id, caption, url, position, width, height}`).

### Article anatomy — the citation goldmine
Two equivalent representations:
- **JSON (`/api/page-preview`):** `.page.content` is Markdown (heading hierarchy
  recoverable from `#`/`##`/`###`); `.page.citations[].url` is the clean
  primary-source URL list. Caveat: on the page inspected, only `url` was
  populated — `title`/`description`/`favicon` were empty strings, so derive
  publisher from the URL domain and don't rely on those fields.
- **SSR HTML (`/page/<slug>`):** headings `<h1>–<h4>` each carry a **stable slug
  `id`** (`id="early-life"`, `id="spacex"`) plus machine-readable `data-toc-id`
  (520 occurrences on the Musk page) / `data-toc-depth` / `data-section`
  attributes — a ready-made TOC tree. Inline citations are `<sup>` markers (1,872
  of them) linking to `#ref-N`. The reference list is `<li id="ref-N"><a
  href="<external URL>" target="_blank">` under `<div id="references">` (830
  external primary-source links: apnews, arstechnica, arxiv, britannica, …). The
  HTML additionally preserves the *sentence → citation* mapping via `<sup>` →
  `#ref-N`, which the JSON does not.

### Discovery & enumeration
- **`robots.txt`** (verbatim):
  ```
  User-agent: *
  Disallow: /api/

  Sitemap: https://assets.grokipedia.com/sitemap/sitemap-index.xml
  ```
- **Sitemap:** `https://assets.grokipedia.com/sitemap/sitemap-index.xml` → 235
  child sitemaps (`sitemap-00001.xml` … `sitemap-00235.xml`), each ~4.3 MB with
  25,000 alphabetical `<loc>https://grokipedia.com/page/<slug></loc>` entries
  (~5.9M URLs). Hosted on the `assets.` CDN host (no Cloudflare challenge in
  testing), separate from the CSP-locked main site. This is the authoritative way
  to enumerate the whole corpus.
- No RSS/Atom feed, no A–Z index page, no category-index endpoint. "Trending"/
  "Recent" appear on the homepage but are hydrated client-side (no hrefs in raw
  HTML). Rank by popularity yourself using `viewCount` (from `/api/typeahead`) or
  `stats.totalViews` (from `/api/page-preview`).

### Freshness & history
- **Freshness is mixed and lagging.** Sitemap `lastmod` is a stale snapshot — do
  not trust it for change detection. Per-page `metadata.lastModified` is reliable
  (Musk page = 2026-04-22 while `statsTimestamp` = today). Content does track
  current events (covers "Starship Version 3", "2025 feud with Trump"), but
  individual pages can be months old. **Use `metadata.lastModified` per page for
  staleness decisions** and surface it to the agent so it knows how current a
  section is.
- **No public committed-revision/diff history.** The only history-like surface is
  `/api/list-edit-requests-by-slug` (pending, un-committed edit *requests* —
  articles are machine-authored, `lastEditor: "system"`; humans submit requests).

### Access reality (Cloudflare)
- `server: cloudflare`, `cf-ray`, and a `__cf_bm` bot-management cookie on every
  response. **Default library User-Agent → HTTP 403; a normal Chrome UA → 200.**
  Send a browser-like UA, carry the `__cf_bm` / `grokipedia-affinity` cookies
  across requests (reuse an `httpx` session), and throttle. Article pages return
  `cache-control: public, max-age=300` (~5 min cacheable). No hard rate limit was
  hit in light testing, but aggressive/parallel scraping risks challenges.

---

## 3. Prior art

### Blueprints to copy
- **`Rudra-ravi/wikipedia-mcp`** — the reference Wikipedia MCP. 10 tools that map
  almost exactly onto our goals: `search`, `get_summary`, `get_sections` (TOC),
  `summarize_article_section` (section drill-down), `get_links`,
  `get_related_topics`, `extract_key_facts`, `get_article` (full-dump escape
  hatch). **Key lesson: no server-side chunking** — it solves context blowups
  with *selective-retrieval tools* (summary / section / key-facts), not by
  pre-chunking. Caching is opt-in; rate-limiting punted to the caller. Ships a
  `test_connectivity` self-diagnostic tool.
- **Grokipedia MCP** (`get_page_sections` / `get_page_section` / `get_page_citations`
  triad) — the exact TOC → section → citations shape for our target source, with
  section-by-header-name (case-insensitive) as the drill-in key.

### Grokipedia clients (open-internet, no key)
- **`AkeBoss-tech/grokipedia-api`** (PyPI `grokipedia-api`, Python + JS) — calls
  the live `/api/page-preview` + `typeahead()` + `get_stats()`. Closest to the
  JSON-endpoint approach; its changelog explicitly notes switching to
  `/api/page-preview`. Returns `title`, `content`, `citations` (`id/title/url`),
  `images`.
- **`jasonniebauer/grokipedia-api`** — HTML scraping via BeautifulSoup over
  `/page/{slug}`; returns full text + numbered citation URLs.
- **`dbccccccc/Grokipedia-api`** — headless-browser real-time scraper (heavier;
  unnecessary given SSR).
- **Apify `clearpath/grokipedia-scraper`** — hosted actor returning structured
  JSON (slug/title/content/images/categories/citations + linked-page slug arrays).
- Background reading: arXiv `2511.09685` ("What did Elon change? A comprehensive
  analysis of Grokipedia") — confirms citations parse as discrete numbered
  elements because the structure is Markdown-derived and simple.

### MediaWiki section pattern (the canonical TOC→section drill-down)
Even though Grokipedia isn't MediaWiki, its pattern is the standard to mirror:
`action=parse&prop=tocdata` → flat section list with `number` ("3.1"), `line`
(title), `anchor`, `toclevel`; then `action=parse&section=N` to fetch just one
node. **Lesson:** represent the outline as a **flat list of
`{number, level, title, anchor}`** (cheaper in tokens than a nested object; the
agent reconstructs hierarchy from `level`/dotted-number; `anchor` is the stable
drill-in ID). Grokipedia hands us exactly this via `data-toc-id` / `data-toc-depth`
+ heading `id` anchors.

### Agent tool-design guidance (Anthropic, "Writing effective tools for agents")
- **Few, high-leverage tools**, not one-per-endpoint. "More tools don't always
  lead to better outcomes."
- **Namespace** tool names (`grokipedia_search`, `grokipedia_section`) to cut selection
  confusion.
- **Return human-readable identifiers** (title, heading number/anchor, url) — not
  opaque UUIDs. The section drill-in key should be the heading title/number.
- **`response_format` enum (`concise` vs `detailed`)** — let the agent choose
  text-only vs text+citations+links (their example: 72 vs 206 tokens).
- **Pagination + helpful truncation:** when cutting off, tell the agent how to get
  more and steer it toward many small targeted fetches over one broad dump.
- **Actionable error messages** with correct-format examples, not opaque codes.

### Extraction & politeness libraries
- **Trafilatura** (already a JBrain2 dep) — best-in-class HTML→Markdown/JSON with
  heading hierarchy + metadata (author/date) preserved. Use only as a fallback;
  Grokipedia's Markdown/structured HTML rarely needs it.
- **Politeness (MediaWiki etiquette, worth mirroring):** contactable User-Agent,
  **serial not parallel** requests, exponential backoff on 429/503, cache by
  URL/slug with a TTL (cache the outline longest — it rarely changes), honor
  `ETag`/`Last-Modified`, single-flight duplicate requests.

---

## 4. How it fits JBrain2 (codebase)

Backend is Python 3.11 + FastAPI + async SQLAlchemy/asyncpg + Postgres(pgvector);
`httpx` for outbound HTTP; `.tool` sidecars discovered by a `ToolRegistry`.

- **`jerv`** (`backend/src/jbrain/agent/agents.py:343`) is a sandboxed web
  chatbot persona: KB-blind, empty read scopes, biggest tool allowlist
  (`JERV_TOOLS`, `agents.py:106`), `budget_multiplier=6`. Its `web`-class tools
  (`web_search`, `web_fetch`) run **directly — no egress Proposal** — the bounded,
  owner-approved exception to invariant #9 (`docs/reference/ASSISTANT.md:505-550`),
  justified because the sandbox holds no owner data. A Grokipedia tool set is a
  natural addition to `JERV_TOOLS`.
- **Tool pattern to copy:** `web_search.tool` sidecar (`permission: web`, JSON-
  Schema `params`, versioned) + its handler in
  `backend/src/jbrain/agent/webtools.py` (`build_web_handlers`, returns
  `ToolOutput("...", web_sources=(WebSource(...),))`). `web_sources` is the
  built-in **citation-surfacing** channel — favicon/title/url chips the UI
  renders. Handlers are `async (args: dict, ctx: ToolContext) -> str|ToolOutput`
  (`loop.py:302`). Registry wired in `readtools.build_registry` +
  `main.py` (alongside `build_web_handlers`).
- **HTTP/scraping already present** (`web/fetch.py`, `web/search.py`): an
  SSRF-guarded `WebFetcher.fetch(url, offset, find)` with `window_text` paging and
  a `guard_public_host(url)` guard (refuses private/loopback/reserved hosts,
  re-checks redirect hops — **use it on any model-influenced URL**); a
  `SearxngClient` over self-hosted SearXNG with a `cachetools.TTLCache`. Deps
  already available: `httpx`, `trafilatura`, `pymupdf`, `cachetools`. **No** `bs4`,
  `playwright`, or `readability` — do not add them.
- **Caching fetched pages:** reuse the cross-turn tool-artifact subsystem
  (`agent/tool_artifacts.py` `ToolArtifactRepo` + `BlobStore`, RLS-firewalled per
  session, with a paging cursor) exactly as `web_fetch` does — heavy article text
  → BlobStore, metadata + cursor in Postgres, the agent pages via `read_artifact`.
- **LLM adapter** (`llm/router.py`, `complete(task, ...)`) is available if a tool
  wants a query-focused section summary; add a task route only if it needs its own
  budget, else pass a `strength` tier. Faked in tests via `FakeLlmClient`.
- **Tests:** unit test per tool module (`backend/tests/unit/test_grok*tools.py`)
  with an injected `httpx` `transport` (no network) + a sidecar-validity test;
  integration/RLS test only if a new table is added (e.g. a page cache).

---

## 5. Recommended tool design

Five namespaced `web`-permission tools for `jerv`, each token-bounded. Drill-in
key = heading title/number (human-readable). This is the agentic loop:
**search → outline → section → citations**, with related-links for traversal.

| Tool | Params | Returns | Notes |
|---|---|---|---|
| `grokipedia_search` | `query`, `limit=6` | ranked `[{slug, title, snippet, view_count, last_modified}]` | Entry point. Rank/annotate with `view_count` + freshness so the agent picks well. |
| `grokipedia_outline` | `slug` | flat tree `[{number, level, title, anchor}]` + `{title, last_modified, category[]}` | The cheap TOC. **Never returns body text.** Cache aggressively. |
| `grokipedia_section` | `slug`, `section` (title/number/anchor), `response_format=concise\|detailed` | section Markdown; `detailed` also returns that section's citations + outgoing wiki-links | The workhorse — pull one node, not the article. Paginate if a section is huge. |
| `grokipedia_citations` | `slug`, `section?`, `limit` | `[{ref, title, url, publisher, date?}]` | The requester's #1 goal. `publisher` derived from URL domain when blank. Agent then `web_fetch`es the `url` to reach the primary source. |
| `grokipedia_related` | `slug`, `limit` | `[{slug, title}]` (linked / same-category pages) | Corpus traversal. |

Plus `grokipedia_article(slug)` — full-dump escape hatch, discouraged in the tool prose
(steer the agent to outline+section instead).

**Response discipline:** default per-section length cap (~5–10k chars, matching
the Grokipedia MCP), pagination + helpful truncation messages, freshness
(`last_modified`) surfaced on outline/section so the agent knows staleness, and
actionable errors ("no page named X; try grokipedia_search").

---

## 6. The one real decision — access surface vs `robots.txt`

`robots.txt` says `Disallow: /api/`. That splits the surfaces:

| Path | robots | Search quality | Citation quality | Cost |
|---|---|---|---|---|
| **`/api/typeahead` + `/api/page-preview`** (JSON) | **Disallowed** | High (native search over 6M pages) | Cleanest (structured `citations[]`, Markdown) | Low (small JSON) |
| **`/page/<slug>` SSR HTML** | Allowed | — (no on-site HTML search) | Full (`#ref-N` list + sentence→cite map) | High (1.76 MB/page, parse server-side) |
| **`site:grokipedia.com` via existing SearXNG** | Allowed | Partial recall, lags the corpus | — (search only) | Low; **already in the repo** |

There is **no robots-allowed on-site search** — search forces the choice.

**Decision (ratified 2026-08-02): API-first.** Default to the `/api/` JSON
endpoints — `/api/typeahead` for `grokipedia_search`, `/api/page-preview` for
outline/section/citations — with **SSR `/page/<slug>` parsing as the automatic
fallback** when an API call fails, drifts, or is Cloudflare-challenged. Rationale:
jerv makes a handful of targeted, human-triggered lookups (not bulk crawling —
the concern `Disallow: /api/` protects against), the same posture as its existing
direct web fetches on the owner's personal system; the `/api/` path gives real
full-corpus search and pre-structured citations, directly serving the primary goal
(fast citation extraction). The robots-compliant SSR path is retained as
belt-and-suspenders, not discarded. See `../../GROKIPEDIA_TOOL_PLAN.md`.

---

## 7. Politeness, caching, failure modes

- **Transport:** one reused `httpx` session with a browser-like User-Agent and a
  persistent cookie jar (`__cf_bm`, `grokipedia-affinity`) to satisfy Cloudflare;
  serial requests; exponential backoff on 429/503. Run any model-influenced URL
  through `guard_public_host`.
- **Caching:** per-slug + per-section TTL cache; **cache the outline longest**
  (structure rarely changes); honor `max-age=300` on pages. Reuse
  `ToolArtifactRepo` + `BlobStore` for large fetched bodies so repeated
  drill-downs are free within a session.
- **Failure modes to handle:** Cloudflare challenge (fall back to SSR page or
  report actionable error — Playwright only as a last resort); `/api/` shape
  drift (community-reverse-engineered endpoints can change — pin behavior with
  transport-injected tests and keep the SSR path as fallback); stale content
  (surface `last_modified`); empty citation `title`/`description` (derive
  publisher from domain).

---

## 8. Sketch of build waves (for a future Proposed plan)

Not a committed plan — a sketch of how it would decompose:

- **W1 — Client + parser.** `web/grokipedia.py` `httpx` client (browser UA, cookie
  jar, backoff) + a parser turning SSR HTML (and/or `/api/page-preview` JSON) into
  `{outline[], section(name), citations[]}`. Unit tests with injected transport
  over saved fixtures. No agent wiring yet.
- **W2 — The 5 `.tool` sidecars + handlers** (`grokipedia_search/outline/section/
  citations/related`), returning `ToolOutput` with `WebSource` citations; wired
  into `build_registry` + `main.py`; added to `JERV_TOOLS`. Sidecar-validity test.
- **W3 — Caching + polish.** Cross-turn artifact caching for large bodies,
  outline TTL cache, `response_format` toggle, freshness surfacing, actionable
  errors; the operator-gated `/api/` fast path if approved in §6. Reconcile
  `docs/reference/ASSISTANT.md` (jerv tool inventory) + `docs/reference/SERVICES.md`.

---

## 9. Open questions / uncertainties

- **Access-surface decision** (§6) — resolved: API-first with SSR fallback.
- `/api/typeahead` max `limit` and total-count semantics unverified; whether
  `citations[].title/description` are ever populated (empty on the one page
  inspected).
- Whether `/api/` enforces a rate limit under real load (only light testing done).
- No confirmed committed-revision-history endpoint (only pending edit-requests via
  `/api/list-edit-requests-by-slug`).
- Grokipedia ToS text was not retrieved — treat automated access as
  tolerated-but-Cloudflare-gated, not explicitly blessed.

---

## 10. Sources

Grokipedia surface verified by direct `curl` (browser UA) on 2026-08-01:
`grokipedia.com/robots.txt`, `/page/Elon_Musk`, `/api/page-preview`,
`/api/typeahead`, `/api/stats`, `assets.grokipedia.com/sitemap/sitemap-index.xml`.

Prior art: `github.com/Rudra-ravi/wikipedia-mcp`,
`github.com/AkeBoss-tech/grokipedia-api` (PyPI `grokipedia-api`),
`github.com/jasonniebauer/grokipedia-api`, `apify.com/clearpath/grokipedia-scraper`,
`wikipedia-api.readthedocs.io`, `mediawiki.org/wiki/API:Parsing_wikitext`,
`mediawiki.org/wiki/API:Etiquette`,
`anthropic.com/engineering/writing-tools-for-agents`,
`trafilatura.readthedocs.io`, arXiv `2511.09685`.

JBrain2 codebase: `agent/agents.py`, `agent/webtools.py`, `agent/toolregistry.py`,
`agent/loop.py`, `agent/tool_artifacts.py`, `web/fetch.py`, `web/search.py`,
`docs/reference/ASSISTANT.md`, `docs/DOC_LIFECYCLE.md`.
