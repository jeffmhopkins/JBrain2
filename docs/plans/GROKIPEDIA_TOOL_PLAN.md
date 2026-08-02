# Grokipedia Tool — Implementation Plan

> **Status:** In progress · **Last verified:** 2026-08-02 · **Waves:** W1✅ W2◻️ W3◻️

Give the `jerv` persona a small set of tools to search Grokipedia, traverse an
article by its table of contents, drill into individual sections without dumping
the whole article into context, and **pull citations so the agent can follow them
to primary sources**. The purpose is fast, cheap access to information on any
subject, news, or current event — with a clear path from a Grokipedia claim to
the source that backs it.

**W1 has landed** (the `jbrain.web.grokipedia` client + parser). It is the
implementation companion to the research in `../research/grokipedia-tool/RESEARCH.md`
(verified public surface, prior art, JBrain2 fit, tool-design rationale) — read
that first for the evidence behind the choices here.

**Access constraint:** no xAI/Grok API key. All access is open-internet. Verified:
this is comfortable — Grokipedia's own first-party endpoints and SSR pages give
everything needed without the paid xAI API surface.

---

## 1. The decision — API-first, SSR-HTML fallback

Grokipedia exposes two usable surfaces (full analysis in the research doc §6):

- **First-party JSON** — `GET /api/typeahead?query=&limit=&offset=` (real search)
  and `GET /api/page-preview?slug=` (Markdown body + structured `citations[]` +
  metadata + images). Cleanest by far. `robots.txt` disallows `/api/`.
- **SSR HTML** — `GET /page/<slug>` carries the whole article: a `data-toc-id`/
  `data-toc-depth` TOC, heading `id` anchors, and a `<li id="ref-N"><a href>`
  reference list of primary-source URLs. `robots.txt`-allowed. Heavy (~1.76 MB).

**Ratified:** **API-first.** Jerv makes a handful of targeted, human-triggered
lookups — not bulk corpus crawling — the same posture as its existing direct web
fetches, on the owner's personal system. The `/api/` endpoints give real
full-corpus search and pre-structured citations, which directly serve the primary
goal (fast citation extraction). **SSR `/page/<slug>` parsing is the automatic
fallback** whenever an `/api/` call fails, returns an unexpected shape (these are
reverse-engineered endpoints and can drift), or is Cloudflare-challenged. Both
paths produce the same internal `{outline, section, citations}` model, so the tool
layer is surface-agnostic.

---

## 2. The tool set (5 tools + escape hatch)

Namespaced `grok_*`, `permission: web`, added to `JERV_TOOLS`. Drill-in key is the
heading title/number (human-readable, per Anthropic tool-design guidance). The
agentic loop is **search → outline → section → citations**, with related-links for
traversal.

| Tool | Params | Returns |
|---|---|---|
| `grok_search` | `query`, `limit=6` | `[{slug, title, snippet, view_count, last_modified}]`, popularity-ranked |
| `grok_outline` | `slug` | flat tree `[{number, level, title, anchor}]` + `{title, last_modified, categories[]}` — **no body text** |
| `grok_section` | `slug`, `section`, `response_format=concise\|detailed` | section Markdown; `detailed` adds that section's citations + outgoing wiki-links |
| `grok_citations` | `slug`, `section?`, `limit` | `[{ref, title, url, publisher, date?}]` (`publisher` derived from URL domain when blank) |
| `grok_related` | `slug`, `limit` | `[{slug, title}]` linked / same-category pages |

Plus `grok_article(slug)` — full-dump escape hatch, discouraged in the tool prose
(steer the agent to outline + section).

**Response discipline:** per-section length cap (~5–10k chars) with pagination and
helpful truncation messages; `last_modified` surfaced on outline/section so the
agent knows staleness (Grokipedia pages can be months old); actionable errors
("no page named X; try grok_search"). Citations surface through `ToolOutput`'s
`web_sources` channel so the UI renders them as followable chips.

---

## 3. Architecture & fit (see research doc §4)

- **Client:** `backend/src/jbrain/web/grokipedia.py` — an `httpx` client mirroring
  `web/search.py`: injected `transport` for network-free tests, one reused session
  with a browser-like User-Agent + persistent cookie jar (`__cf_bm`,
  `grokipedia-affinity`) for Cloudflare, serial requests with exponential backoff,
  failures wrapped in a `GrokipediaError`. Any model-influenced URL passes through
  `web/fetch.py`'s `guard_public_host`.
- **Parser:** turns `/api/page-preview` JSON (primary) or SSR HTML (fallback) into
  one internal `{outline[], section(name), citations[]}` model. HTML fallback
  reuses `trafilatura` only where needed; heading tree comes from `data-toc-id` +
  anchors, citations from the `#ref-N` list.
- **Handlers:** `build_grokipedia_handlers(...)` returning `ToolOutput(text,
  web_sources=(WebSource(...),))`, merged into `readtools.build_registry` and
  wired in `main.py` alongside `build_web_handlers`.
- **Caching:** per-slug/per-section TTL cache (outline cached longest — structure
  rarely changes; honor page `max-age=300`); large bodies via the cross-turn
  `ToolArtifactRepo` + `BlobStore`, exactly as `web_fetch` does.
- **Zero new runtime deps** (`httpx`, `trafilatura`, `cachetools` already
  present). If any dep were added, `scripts/dev-setup.sh` updates in the same PR.

---

## 4. Waves

- **W1 — Client + parser.** ✅ `backend/src/jbrain/web/grokipedia.py` — the
  `GrokipediaClient` (API-first `/api/typeahead` + `/api/page-preview`, SSR
  `/page/<slug>` fallback, browser UA + persistent Cloudflare cookie jar) and the
  surface-agnostic parser producing the `GrokArticle` `{outline, section,
  citations, related}` model (hierarchical section numbers + anchors, section
  drill-down by number/anchor/title, citations with domain-derived publisher).
  Unit tests (`backend/tests/unit/test_grokipedia.py`, injected transport, no
  network) cover both surfaces, the fallback path, the 404 no-fallback rule, and
  cookie persistence. No agent wiring yet.
- **W2 — Tools.** The five `.tool` sidecars + handlers, `ToolOutput`/`WebSource`
  citations, registry wiring, added to `JERV_TOOLS`. Sidecar-validity test + a
  multi-turn loop test driving search→outline→section→citations with the fake
  adapter.
- **W3 — Caching + polish + docs.** Cross-turn artifact caching, outline TTL
  cache, `response_format` toggle, freshness surfacing, actionable errors.
  Reconcile `docs/reference/ASSISTANT.md` (jerv tool inventory) +
  `docs/reference/SERVICES.md`; on ship, `git mv` this plan to `archive/` and carry
  residuals to `ROADMAP.md`.

---

## 5. Testing (CLAUDE.md #5)

Tests land with the code: 80% backend coverage, real Postgres via testcontainers
only where a table is touched, LLM calls faked. `backend/tests/unit/
test_grokipedia*.py` exercises the client (injected `httpx` transport, no network)
and parser over fixtures, plus a sidecar-validity test. No new table is planned
(caching reuses the existing RLS-firewalled tool-artifact substrate); if one is
added, an RLS isolation test is mandatory.

---

## 6. Risks

- **`/api/` shape drift** — reverse-engineered endpoints can change without
  notice. Mitigation: SSR fallback + fixture-pinned parser tests; alert on parse
  failure rather than returning silent garbage.
- **Cloudflare escalation** — if challenges appear beyond UA/cookie, fall back to
  SSR page fetch; Playwright only as a last resort (would be a new dep — a
  separate decision).
- **Staleness** — pages can lag current events by months; surface `last_modified`
  so the agent (and user) can judge, and prefer `web_search` for truly breaking
  news.
- **Empty citation metadata** — `title`/`description` are often blank; derive
  `publisher` from the URL domain and let the agent fetch the `url` for the rest.

---

## 7. Sources

Companion research: `../research/grokipedia-tool/RESEARCH.md` (Grokipedia public
surface verified 2026-08-01; prior art — `wikipedia-mcp`, Grokipedia clients,
MediaWiki TOC pattern, Anthropic tool-design guidance; JBrain2 codebase fit).
Governing precedent for a direct `web`-class tool: `docs/reference/ASSISTANT.md`
(jerv sandbox, the bounded web exception to invariant #9).
