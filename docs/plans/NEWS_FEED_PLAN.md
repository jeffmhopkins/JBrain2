# JBrain2 — News Feed Tool Plan

> **Status:** In progress · **Last verified:** 2026-08-12 · **Waves:** A✅ B◻️ C◻️

A curated RSS/Atom feed source (`news_feed`) for jerv's gather, so the daily-news
brief's discovery step stops depending on the search engines that throttle a
residential IP.

Peer to `REPORT_PRESET_PLAN.md` (the `daily_news` brief this serves) and
`CHALLENGE_SOLVER_PLAN.md` (the fetch-tier work this complements). Grounded in three
research memos (RSS design, news-API landscape, web-fetch upgrades) that converged on
one insight: **discovery and body-reading are separate problems that fail for
different reasons.** Body-reading is already handled by the reader→byparr escalation.
Discovery — finding fresh, real article URLs — is what actually throttles on the box's
residential IP: SearXNG's `news` category fans out to Google/Bing News, the exact
upstreams that 429/403 a home box. Curated per-category feeds sidestep that.

## The problem

A live `daily_news` deep-research run spent ~19 minutes in the gather/read phase,
much of it on a throttled google-news discovery leg and ~20 bot-walled fetches. The
news brief's five angles (national, economy/world, space, AI/tech, local Space Coast)
each searched the news engines for article URLs; on a residential IP that search leg is
the slow, rate-limited part.

## The design

`news_feed(category, since, limit)` pulls a hand-picked set of RSS/Atom feeds per
topic category, filters to a recency window, dedupes, and returns dated article leads
newest-first — with, for the feeds that carry `content:encoded` (NASA, Spaceflight Now,
Space.com, Space Coast Daily), the **full article body inline**, marked "✓ full text".

Two structural facts drive the whole design:

1. **Egress stays in the one guarded place.** `feedparser.parse()` would do its own
   network I/O, bypassing the SSRF guard, the browser headers (why a bare UA gets 403'd),
   and the byte cap. So the raw feed bytes are fetched through a new
   `WebFetcher.fetch_feed()` (reusing `_send_following_safe_redirects` + `_read_capped`),
   and `parse_feed()` is a **pure offline function over those bytes** — deterministic and
   testable with a `MockTransport`, exactly like every other web client.

2. **Full-body vs summary is the load-bearing split.** A full-body item is returned WITH
   its extracted body (through the existing `_extract_html`/trafilatura path, so it reads
   like a fetched page) and a `WebSource(read=True)` — an already-opened article. A
   summary-only feed (NPR, BBC, PBS, TechCrunch — they truncated their feeds; AP has no
   feed) returns a headline + lead + `WebSource(read=False)` — an unverified lead, like a
   `news_search` hit the reader must open.

### Where it plugs into the morning brief

The `daily_news` preset uses the two-phase **scout → read** gather (it sets
`min_reads`). That path deliberately separates discovery (the `research_scout`, whose
prose is discarded — only the URLs it touched survive) from reading (the
`research_fetch` reader, which is search-less by design so it can't wander, guarded by a
test). That separation is why the integration is phased:

- **Wave A (this plan, shipped): the tool + fast discovery.** `news_feed` is held by
  `jerv`, `research`/`review`/`research_deep`, and `research_scout` — **not** the reader
  (adding a discovery tool there would break its no-wander invariant). The preset's angle
  briefs steer the scout to call `news_feed` first, so the throttled google-news
  discovery leg is replaced by clean, dated, curated article URLs the reader then opens.
  In the **flat gather** and **direct-jerv** paths (where the calling agent's prose
  survives), the full-body items are used immediately — findings are written straight
  from the inline body with no fetch.

- **Wave B (deferred): full-body injection into the two-phase reader path.** In the
  two-phase path the scout's prose is discarded, so a full-body item's text does not yet
  reach the writer — the reader re-fetches the URL. Wave B injects full-body feed items
  as synthetic gather findings (the mechanism `_emr_seed_child` already uses), driven by
  a preset-declared category list, so the space/local angles skip the reader fetch
  entirely. Isolated to `deep_research.py` + `research_presets.py`, landed and measured
  separately.

- **Wave C (deferred): owner-editable feeds + on-box tuning.** Wave A ships working
  curated defaults in `config.py` (no operator action needed to use it). Wave C exposes
  the per-category feed list in **PWA Settings** (a `news_feeds` key on the generic
  `app.settings` store — no migration) so the owner curates feeds with no terminal
  (CLAUDE.md #10), plus live cadence/threshold tuning after a real run.

## Curated default feeds (Wave A)

Verified live 2026-08-12; FULL = carries `content:encoded`. NASASpaceflight is dropped
(its feed itself 403s — as bot-walled as its HTML).

| Category | Feeds | Body |
|---|---|---|
| `space` | NASA, Spaceflight Now, Space.com | FULL |
| `ai_tech` | TechCrunch, The Verge, Electrek | summary |
| `national` | NPR News, PBS NewsHour | summary |
| `economy` | BBC Business, NPR Business | summary |
| `world` | BBC World, NPR World | summary |
| `local` | Space Coast Daily | FULL |

National/world lean on summary feeds + the reader (AP has no feed — `news_search` stays
the fallback there). Reuters/WSJ/Bloomberg hard paywalls remain unfetchable by anything
— the RSS-can't-fix ceiling.

## Wave A surface (this PR)

- `web/feeds.py` — `FeedItem`, `FeedClient` (injectable clock + `TTLCache`), pure
  `parse_feed(bytes)`.
- `web/fetch.py` — `WebFetcher.fetch_feed(url)` (guarded GET → raw bytes).
- `agent/webtools.py` — `news_feed_tool` in `build_web_handlers` (new `feeds` param).
- `agent/tools/news_feed.tool` — the sidecar (v1, no `enum` — gpt-oss grammar).
- `agent/agents.py` — `news_feed` added to `JERV_TOOLS`, `RESEARCH_TOOLS`, `SCOUT_TOOLS`
  (NOT `FETCH_TOOLS`). Unbudgeted (curated feeds, not the throttled upstreams the scout
  search budget guards).
- `config.py` — `news_feeds` curated default map.
- `main.py` — construct `FeedClient`, pass to `build_web_handlers`.
- `agent/presets/daily_news.preset` — angle briefs call `news_feed` first.
- `feedparser` dependency (`pyproject.toml`, `dev-setup.sh` note, `test_feed_deps.py`).
- Tests: `test_feeds.py` (parse/client/window/dedupe/best-effort), handler tests in
  `test_web.py`, persona pins in `test_agents.py`, sidecar digest pin in
  `test_agent_readtools.py`.

## Tradeoffs

- **Curation is ongoing maintenance** the SearXNG path didn't have — feeds move and flip
  full↔summary (TechCrunch/Electrek already truncated theirs). Wave C's owner-editable
  list plus sane defaults is the mitigation.
- **Discovery win, not a paywall cure.** It removes the discovery throttle and (Wave B)
  the full-text-outlet fetches; it does not defeat hard paywalls.
