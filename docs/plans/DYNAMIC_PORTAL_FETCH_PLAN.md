# Dynamic Portal Fetch — resolvers for JS/POST government search portals ("dinosaurs")

> **Status:** In progress (adversarial review incorporated — see "Review outcomes" below) ·
> **Last verified:** 2026-08-09 · **Wave order (revised):** P3✅ → P1✅ → P2⏸️ (deferred — see below)
> (the dependency-free honesty backstop shipped FIRST; see Review outcomes R-Q1)

Give the research fan a way to actually *query* dynamic government search portals — the
ones `web_fetch` can only see the empty search FORM of — and make an un-queryable portal
degrade honestly instead of laundering "the search returned nothing" into "no record
exists." Two complementary capabilities: **portal resolvers** (Wave P1–P2) turn a name
into real result rows via each portal's *actual* result/detail endpoint, and a
**"this is a search form, not results" detector** (Wave P3) is the honest-degradation
backstop for every portal we don't (yet) have an adapter for.

## Motivation — the candidate-accountability failure we diagnosed

A `candidate_profile` preset run (`deep_research` → a fan of `research` sub-agents, each
holding only `web_search` / `web_fetch` / `current_time`) failed on facts locked behind
dynamic portals:

- **FL Sunbiz corporation search.** A researcher fetched
  `search.sunbiz.org/Inquiry/CorporationSearch/ByName?search_term=…`. That URL is the
  search **form**, not the results page. `web_fetch` cannot run the form's client-side
  search, so it returned the form HTML — and the model concluded "no entity found."
- **FL DFS DICE public-adjuster license search.** `dice.fldfs.com/public/pb_*.asp` is an
  ASP form that answers a POST. A plain GET returns only the form; the run again read
  "nothing" as "no license."

The model then upgraded **"the portal returned nothing readable" → "no record exists"** —
exactly the failure `candidate_profile.preset` already warns against in prose ("AN EMPTY
PORTAL IS NOT PROOF OF ABSENCE"). But prose is all it had: the *tool* gave the agent no
way to actually query these portals, so the guidance had nothing to redirect to.

Note the asymmetry that shapes the design: the DFS **licensee-detail** page by numeric id
(`licenseesearch.fldfs.com/Licensee/<id>`) IS static and already fetchable. The gap is
narrowly the **search step** that turns a name into that id / into result rows. Resolvers
close that step and hand the fan a fetchable/citable detail URL; the fan then reads and
cites it through the existing `web_fetch` path, unchanged.

## What exists today (the seams we build on)

- **`web_fetch` / `WebFetcher`** (`backend/src/jbrain/web/fetch.py`,
  `backend/src/jbrain/agent/webtools.py`). The one outbound leg of the jerv sandbox. It
  already carries every egress guarantee a resolver needs: a per-hop **SSRF guard**
  (`guard_public_host` + `_send_following_safe_redirects` — httpx auto-redirect off, every
  hop's host re-validated), size caps (`_read_capped`), browser headers, a **POST path**
  (`_fetch_post`, form-encoded or JSON, same SSRF discipline), and the reader/solver
  recovery tiers. It also already has a precision-tuned interstitial detector,
  `_is_challenge_page`, whose seams (raise on direct, None on reader/solver so nothing
  becomes a cited source) are the exact pattern Wave P3 mirrors.
- **The `public_records` umbrella** (`backend/src/jbrain/agent/publicrecordstools.py`,
  `backend/src/jbrain/agent/tools/public_records.tool`, clients under
  `backend/src/jbrain/web/`: `nppes.py`, `federal_register.py`, `public_records.py`,
  `wikidata.py`). This is the **template** for what we are adding: ONE `web`-permission
  tool that fans a name across a **registry of pinned, keyless public sources**, each a
  small client with a config-pinned `base_url`, a `configured` property, and a `(rows, ok)`
  return so an outage degrades rather than raising. Hits surface as `WebSource` chips. A
  resolver registry is the same shape, one step deeper (it also parses the result page and
  returns row-level detail URLs).
- **The citation registry** (`deep_research._collect_sources`, `_ChildResult.web_sources`,
  `contracts.WebSource`). A sub-agent's tool calls capture the real URLs it reached; those
  ride up (`_ChildResult.web_sources`) and dedupe into the run's global `[^n]` registry.
  Anything a resolver surfaces as a `WebSource` flows into that registry for free — a
  resolver row becomes a numbered, favicon-chipped, `web_fetch`-able citation with no
  pipeline change.
- **The parent⊆child clamp** (`spawn.effective_child_tools`). A child holds at most
  `persona.tools ∩ parent.agent_tools`. So a new `portal_search` tool must be granted to
  **both** jerv (`JERV_TOOLS`) and the research persona (`RESEARCH_TOOLS`) for a research
  child to hold it; the library/reports personas stay no-web and never receive it.
- **Config + wiring** (`config.py`, `main.py`). Each public source has a config `*_url`
  (empty ⇒ client disabled ⇒ handler reports "not configured"); clients are built on
  `app.state` and merged into `web_handlers`, sharing the one `WebFetcher` instance.

## Design

### The resolver abstraction and where it lives

A **portal resolver** is a small adapter that, given a **name** and a **jurisdiction**,
issues the portal's *real* result/search request through the shared egress path, parses
the result page, and returns structured rows — each carrying a fetchable **detail URL**
the research agent can then `web_fetch` and cite.

New package `backend/src/jbrain/web/portals/` (mirrors the per-source client placement
under `jbrain/web/`):

- `base.py` — the contract, the row/result dataclasses, and the registry:

  ```python
  @dataclass(frozen=True)
  class PortalRow:
      title: str            # entity / person name as the portal reports it
      subtitle: str = ""    # status, doc number, license type/number, filing date …
      detail_url: str = ""  # the STATIC, fetchable detail page (the citation target)
      identifiers: tuple[tuple[str, str], ...] = ()  # (label, value): doc no., NPI-like id, etc.

  @dataclass(frozen=True)
  class PortalResult:
      rows: tuple[PortalRow, ...]
      ok: bool              # False ONLY on a source outage/parse failure (vs. a real empty result)
      note: str = ""        # e.g. "results truncated at N", "portal returned a challenge page"

  class PortalResolver(Protocol):
      key: str              # dispatch key, e.g. "fl_business" / "fl_license"
      jurisdiction: str     # "FL"
      kind: str             # "business" | "license"
      label: str            # human header, e.g. "Florida Sunbiz corporation search"
      @property
      def configured(self) -> bool: ...
      async def search(self, fetcher: WebFetcher, name: str, *, limit: int) -> PortalResult: ...
  ```

  A module-level `PORTAL_RESOLVERS: tuple[PortalResolver, ...]` is the registry; adding a
  portal is a one-file change (write the adapter, append it to the tuple). Dispatch is by
  `(jurisdiction, kind)` — or by the explicit `key` when the caller names one.

- `fl_sunbiz.py`, `fl_dfs.py` — the first two adapters (below).

Each adapter is a pure function over `(WebFetcher, name)` → `PortalResult`; the HTTP goes
through the injected `WebFetcher`, so adapters are trivially unit-testable with the same
injected `httpx` transport the existing web clients use, and hold no network of their own.

### Egress: resolvers ride the existing SSRF-guarded path (non-negotiable)

Resolvers **must not** open a new outbound leg. They fetch through `WebFetcher`, reusing
its per-hop SSRF guard, redirect re-validation, size caps, and browser headers verbatim.
`_extract_html` today returns *extracted markdown* (lossy for row parsing), so Wave P1
adds one narrow primitive to `WebFetcher`:

```python
async def fetch_html(self, url, *, method="GET", body="", content_type=...) -> tuple[str, str]:
    """Return (final_url, raw_decoded_HTML/JSON) for a portal result page, through the SAME
    _send_following_safe_redirects SSRF guard + _read_capped size cap + BROWSER_HEADERS as
    fetch(). No trafilatura extraction (the adapter parses rows itself); no reader/solver
    escalation in v1. Raises WebFetchError exactly as fetch() does."""
```

This keeps **all** egress inside `WebFetcher` — the SSRF guard, the byte cap, the redirect
re-validation, and the proxy posture apply identically whether the model called `web_fetch`
or a resolver did. Because the portal host is *pinned by the adapter* (never model-supplied)
and *also* SSRF-guarded, it is belt-and-suspenders: a resolver can neither be pointed at
the box's own services nor 30x its way there. Adapters parse the returned HTML with the
existing dependency-free `jbrain.htmltext` helpers (plus a small bounded row parser) — **no
new HTML/JS dependency** (see Non-goals: no headless browser in v1).

### The two v1 adapters

- **`fl_sunbiz` (FL business registry, kind=`business`).** Builds the **SearchResults**
  request (`search.sunbiz.org/Inquiry/CorporationSearch/SearchResults?…`) the ByName form
  actually issues — not the form URL the failing run fetched — parses the results table
  (entity name, status, document number), and returns one `PortalRow` per entity with a
  `detail_url` pointing at the static
  `SearchResultDetail?inquirytype=…&documentNumber=…` page the agent can fetch and cite.
- **`fl_dfs` (FL DFS licensee lookup, kind=`license`).** Runs the DICE search step the
  bare GET can't — the documented POST the `pb_*.asp` form issues — parses the returned
  rows, and returns one `PortalRow` per licensee whose `detail_url` is the **static**
  `licenseesearch.fldfs.com/Licensee/<id>` page (the id→detail asymmetry from the trace).
  This is the exact "name → id → static detail" bridge the run was missing.

Both are server-rendered (form → server-rendered results table), so a raw guarded fetch +
parse is sufficient; neither needs JS execution.

### The `portal_search` tool and how output feeds the citation registry

A new `web`-permission tool `portal_search(name, jurisdiction, kind=…, limit=…)`,
mirroring `public_records` in construction (`backend/src/jbrain/agent/portaltools.py`,
`build_portal_handlers(fetcher, resolvers, emit=…)`, sidecar
`backend/src/jbrain/agent/tools/portal_search.tool`, `permission: web`). It:

1. Selects resolver(s) by `(jurisdiction, kind)` (or explicit `key`); an unknown/empty
   selection is **steered**, not silently widened — same pattern as `public_records`
   rejecting a bad `sources`.
2. Calls `resolver.search(fetcher, name, limit=…)`.
3. Renders rows as text for the model **and** returns each `detail_url` as a
   `WebSource(url=detail_url, title=row.title, read=False)` in `ToolOutput.web_sources`.

Because the handler returns `WebSource`s, the research child's loop captures them into
`_ChildResult.web_sources` and `deep_research._collect_sources` folds them into the run's
global `[^n]` registry — the resolver row is now a real, numbered, `web_fetch`-able
citation with **zero** change to `deep_research.py` or the synth prompt. `ok=False` (an
outage) renders a distinct "portal unavailable — try another path / web_search" message;
`ok=True` with no rows renders "searched, no match under this name" (which the fan already
knows to treat as *not found here*, not *no record exists*).

### The "search form, not results" detector (P3 — honest-degradation backstop)

For every portal we have **no** adapter for, `web_fetch` must stop letting an empty search
form read as content. A new precision-tuned detector `_is_search_form_page(title, text,
html)` in `fetch.py`, a sibling of `_is_challenge_page`:

- **Fires** when the page's main content is an interactive search form and there is no
  substantive article/result text: raw-HTML form signal (a `<form>` with text/select
  search inputs + a submit control, near-empty extracted body) combined with the
  short-page gating `_is_challenge_page` uses. Precision over recall — a false positive
  drops a real page, so a nav search box on a content-rich page must **not** trip it.
- **On a True verdict**, `web_fetch` returns a **distinct, model-visible observation** —
  not the form text, and not a bare "no readable text" — e.g.: *"That URL is an interactive
  search form, not results — your query was not executed, so this is NOT evidence the
  record is absent. If this is a known government portal, use `portal_search`; otherwise
  find the portal's result/detail URL, a cached copy, or an official index."* It also
  records the URL in `ctx.failed_fetches` so a re-fetch short-circuits.

Wired identically to the challenge detector: the direct path surfaces the observation; the
reader/solver render paths return None if what came back is still a form — so a search form
never becomes a `WebSource` or a cited source.

## Waves

Each wave is independently shippable and CI-green on its own. Portal HTTP is **faked in
every test** (injected `httpx` transport with captured fixture HTML) — tests never hit a
live government site.

### Wave P1 — Resolver framework + FL Sunbiz adapter ✅ (shipped)

**Scope.** The `jbrain/web/portals/` package (`base.py` contract + registry + dataclasses),
the `fl_sunbiz` adapter, `WebFetcher.fetch_html` (the guarded raw-HTML/POST primitive), the
`portal_search` tool + `build_portal_handlers`, config `sunbiz_url`, `main.py` wiring, and
the grants: add `portal_search` to `JERV_TOOLS` and `RESEARCH_TOOLS` (so `research` and
`research_deep` hold it; library/reports personas stay no-web). Reconcile docs that travel
with this code: bump the `research.prompt` and `jerv.prompt` version + content digest and
add one clause pointing the fan at `portal_search` for known gov portals (and *never*
upgrade an empty portal to "no record"); update `docs/reference/ASSISTANT.md` ("Agent
selection") for the new tool.

**Acceptance.**
- `portal_search(name="…", jurisdiction="FL", kind="business")` returns parsed Sunbiz
  entity rows, each with a static `SearchResultDetail…` `detail_url` surfaced as a
  `WebSource`; the failing ByName-form URL is never what the resolver fetches.
- The resolver reaches the portal **only** through `WebFetcher` — verified by a test that a
  resolver fetch to a host resolving private / a redirect to a private host is refused by
  the shared SSRF guard (security path).
- `ok=False` (outage) and `ok=True`/empty (no match) render as distinct messages; neither
  reads as "no record exists."
- An empty `sunbiz_url` disables the adapter and the tool reports it "not configured."

**Tests** (`backend/tests/unit/`, real Postgres not needed — the tool is stateless like
`public_records`, persists nothing, adds no table):
- `test_portals.py` — `fl_sunbiz.search` parses a captured results-table fixture into
  `PortalRow`s with correct `detail_url`s; a form-only fixture yields `ok=True, rows=()`; an
  HTTP error / malformed body yields `ok=False`; the registry dispatches by
  `(jurisdiction, kind)` and by `key`.
- Extend `test_web.py` — `WebFetcher.fetch_html` runs the per-hop SSRF guard (GET + POST),
  re-validates redirect hops, honors the size cap, and returns raw HTML (no extraction).
  **Security path → 100%.**
- `test_portaltools.py` — `portal_search` returns `WebSource`s for each `detail_url`;
  unknown `(jurisdiction, kind)` is steered; unconfigured resolver reports it; the emit
  tendril fires.
- `test_agents.py` — `portal_search ∈ RESEARCH_TOOLS ∩ JERV_TOOLS`, so the clamp passes it
  to a `research` child; it is absent from library/reports personas.

### Wave P2 — FL DFS licensee adapter + registry extensibility ⏸️ DEFERRED

**Deferred after a full reverse-engineering pass (2026-08-09) — the DFS licensee portal is
automation-hardened and cannot be driven by a guarded GET/POST.** This is NOT the "guess the
endpoint" risk R1 warned about; the endpoint and its flow were fully mapped against the live site,
and the finding is a harder gate:

- The real search is `POST https://licenseesearch.fldfs.com/Home/GetLicenseeSearchListPartialView`
  (driven by `/Scripts/MainSearch/LicenseeSearch.js`), returning a results *partial view*. The
  detail pages are the static `…/Licensee/<id>` we already fetch — so the name→id→detail SHAPE is
  as R1 hoped.
- BUT the POST is gated by a **custom, session-correlated `csrf_token`** (a hidden field the page
  injects, matched to the `ASP.NET_SessionId`; a stale/absent token returns `{"redirect":"/"}` —
  the JS comment literally says "CSRF token expired"). A single-session GET→extract-token→POST
  clears that gate (verified: HTTP 200).
- **However, even a valid-token POST returns a rows-EMPTY partial (~1259 bytes, just the export
  script) for EVERY query** — by name (`Collige`), by a common name (`Smith`), and by the exact
  license numbers `W690060`/`W818802`. The server refuses to execute the query for a non-browser
  client, consistent with the Google "unusual traffic" bot interstitial hit on `/Licensee?lastName=`.

So the portal only runs its search for a real browser executing the page's JS/bot-scoring. Driving
it needs a **scripted headless browser** (load page → fill form → submit → read the results
partial), which is beyond BOTH this plan's v1 "no headless browser" Non-goal AND the current
solver tier's capability (Byparr/FlareSolverr `request.get` renders a URL; it does not fill+submit
a form). Sunbiz (P1) shipped because its `SearchResults` is a clean server-rendered GET; DFS is a
different, hardened class of portal.

**What still protects the owner in the meantime:** P3's "search form, not results" detector already
makes an un-queryable DFS fetch degrade honestly (never "no record"), and the static
`…/Licensee/<id>` detail page is fetchable+citable once an id is known. The P1 framework keeps
`fl_dfs` a one-file add the day the scripted-browser capability exists.

**Follow-on (a separate, larger effort — not this plan's v1):** add a scripted-browser egress tier
(or a cookie-carrying GET→POST primitive plus whatever clears the bot-score) and then write
`fl_dfs` against the `GetLicenseeSearchListPartialView` flow documented above, gated on a captured
fixture whose results row carries the `/Licensee/<id>` id (the R1 gate, still binding).

---

_Original P2 scope (retained for the follow-on):_

**Scope.** The `fl_dfs` adapter (the DICE POST search → licensee rows with the static
`licenseesearch.fldfs.com/Licensee/<id>` `detail_url`), config `dfs_dice_url` /
`dfs_licensee_url`, and generalizing the tool so the description advertises the available
`(jurisdiction, kind)` capabilities from the registry (so the model discovers what it can
resolve). Reconcile `candidate_profile.preset`: in the "Funding / business" and
"Controversies & legal record" angles, name `portal_search` as the concrete step for the
state business registry and state license lookups the prose already gestures at, tightening
"AN EMPTY PORTAL IS NOT PROOF OF ABSENCE" into an actionable instruction. Bump the
`public_records.tool`/`portal_search.tool` versions as needed.

**Acceptance.**
- `portal_search(name="…", jurisdiction="FL", kind="license")` runs the DFS search step
  (POST via `WebFetcher.fetch_html`), returns licensee rows each with a static Licensee
  detail URL, surfaced as `WebSource`s — closing the exact name→id→detail gap from the
  trace.
- Adding a portal is demonstrably a one-file change: the new adapter appears in the tool's
  advertised capabilities and dispatches, with no edit to `deep_research.py` or the handler
  core.
- The candidate-profile angles now instruct the fan to use `portal_search` for FL business
  + license before asserting any absence.

**Tests.**
- `test_portals.py` — `fl_dfs.search` parses a captured DICE results fixture into rows with
  correct Licensee `detail_url`s; the POST path is exercised through the faked transport;
  outage/empty distinction holds.
- `test_portaltools.py` — the multi-adapter registry dispatches business vs. license; the
  advertised-capabilities string reflects the registry; a `kind` with no configured
  resolver is steered.
- `test_research_presets.py` — `candidate_profile` renders with the `portal_search`
  instruction present in the relevant angles.

### Wave P3 — "Search form, not results" detection backstop ✅ (shipped)

**Scope.** `_is_search_form_page` in `fetch.py` + the distinct model-visible observation,
wired into `_fetch_direct` (raise-and-surface) and the reader/solver paths (return None on
a still-a-form render), plus the `ctx.failed_fetches` memo. Update `web_fetch.tool` (bump
version) and `research.prompt` (bump digest) so the model reads the new observation as *the
query was not executed*, points at `portal_search`, and never concludes absence from it.

**Acceptance.**
- A fetch of a bare portal search form (the original Sunbiz/DFS ByName/`pb_*.asp` URLs)
  returns the distinct "this is a search form, not results — not evidence of absence"
  observation, **not** the form text and **not** "no readable text."
- **Precision:** a content-rich page that merely has a nav/site search box, a real article,
  and a genuine "no results found" results page are **not** flagged (each a fixture test).
- A flagged form never becomes a `WebSource`/cited source (mirrors the challenge-page
  seams); a repeat fetch of the same URL short-circuits via `failed_fetches`.

**Tests.**
- Extend `test_web.py` — `_is_search_form_page` fires on form fixtures (Sunbiz ByName, DFS
  DICE, a generic gov search form) and does **not** fire on article / nav-search /
  genuine-empty-results fixtures; `web_fetch` surfaces the distinct observation and records
  the failed-fetch memo; reader/solver return None on a still-a-form render.

## Composition with existing invariants

- **SSRF / egress (CLAUDE.md #2 storage-abstraction-adjacent, and the sandbox egress
  rule).** Every resolver byte goes through `WebFetcher._send_following_safe_redirects` —
  the same per-hop host re-validation, byte cap, and browser posture as `web_fetch`. No new
  outbound leg, no unguarded httpx (unlike the pinned public-records clients, this path is
  *doubly* guarded because the SSRF check still runs on the pinned host). P3 adds no egress.
- **LLM adapter (CLAUDE.md #1).** Resolvers and the detector are pure HTTP+parse; no LLM
  call is introduced. No provider SDK touched.
- **jerv sandbox / no owner data.** `portal_search` takes only a name + jurisdiction +
  kind — no owner/KB/health data, exactly like `public_records`. It is `web`-gated and only
  jerv + the web research personas hold it; the library/reports (no-web) personas never
  receive it, so a records-grounded or reports-only run cannot reach a portal.
- **Parent⊆child clamp (parent⊆child).** Granting `portal_search` to `JERV_TOOLS` **and**
  `RESEARCH_TOOLS` is what lets the clamp pass it to a research child; nothing widens a
  child past jerv.
- **Citation registry / feeding waves.** Resolver output enters the run's `[^n]` registry
  purely by being a `WebSource`; the data/instruction boundary is unaffected (a portal row
  is data the writer cites, never instruction).
- **Docs travel with code (CLAUDE.md #9, DOC_LIFECYCLE).** Each wave bumps this plan's wave
  markers, reconciles `ASSISTANT.md`, and — because prompts/tools carry version + digest
  pins enforced in CI — bumps every touched `.prompt`/`.tool`.
- **`dev-setup.sh` (CLAUDE.md #8).** No new dependency (reuses `httpx`, `WebFetcher`, and
  `jbrain.htmltext`), so no setup change — stated explicitly so a reviewer confirms the
  zero-new-dep goal held.

## Non-goals (v1)

- **No headless-browser / JS-execution engine.** The two v1 portals are server-rendered
  (form → server-rendered results), reachable via a documented GET/POST. `WebFetcher`
  already has reader + solver tiers for the genuinely JS-only / bot-walled case; wiring a
  resolver into those tiers is a deliberate follow-on, not v1. Adding a browser engine would
  be a new heavyweight dependency against the zero-new-dep goal, unjustified by these two
  portals.
- **No nationwide coverage.** Two FL portals plus an abstraction built for growth — not a
  generic "scrape any portal." Every adapter is a pinned, reviewed, tested module.
- **No new table / no persistence / no RLS surface.** `portal_search` is stateless like
  `public_records`; resolver rows flow through the existing in-memory citation registry.
  (Hence no testcontainers/RLS test is required for this plan — noted so a reviewer doesn't
  expect one.)
- **No verdicts.** A resolver hit is a LEAD to verify against its primary detail document
  (common-name collisions), same discipline as `public_records`; the tool never asserts a
  match belongs to the subject.

## Risks & mitigations

- **Portal HTML / endpoint drift.** A parser breaks when a portal restyles. Mitigation:
  fixtures pin the parse; `ok=False` on a parse failure degrades to the P3 detector +
  `web_search` fallback rather than silently returning `[]` (which would re-create the "no
  record" lie). Adapters are small and independently replaceable.
- **Anti-bot walls on portals.** A portal may 403/Cloudflare a raw fetch. Mitigation:
  `WebFetcher`'s browser headers already reduce this; a follow-on can opt an adapter into
  the reader/solver tiers. In the meantime `ok=False` degrades honestly.
- **P3 false positives dropping a real page.** A too-eager form detector discards content.
  Mitigation: precision-first gating (short-page + main-content-is-a-form), the explicit
  nav-search / real-article / genuine-empty-results negative fixtures, and the same
  "never flag legitimate prose" bar `_is_challenge_page` holds.
- **False "found" from a namesake row.** A resolver row for a different same-named entity.
  Mitigation: rows are LEADS with a fetchable detail URL; the fan verifies against the
  detail page before citing (the existing research-persona discipline), and the tool text
  says so.
- **ToS / scraping posture.** These are public records reached via each portal's own
  documented query path with a descriptive posture, name-only, low volume — consistent with
  the existing `public_records` sources. Called out for reviewer awareness.

## Open questions for a reviewer

1. **Wave ordering.** P3 (the honesty backstop) is dependency-free and the highest-value
   *lie-stopping* fix on its own — should it ship **first** (before the resolver framework)
   so "empty portal → no record" stops immediately, with resolvers layered on after? The
   plan orders it last (backstop after the primary fix); the owner may prefer the honesty
   fix to lead.
2. **`fetch_html` vs. extend `fetch`.** Is a new `WebFetcher.fetch_html` primitive the
   right seam, or should resolvers instead consume `FetchResult.links` + text from the
   existing `fetch()` (lossier row↔detail-URL association)? The plan chooses `fetch_html`
   for parse fidelity; a reviewer should confirm the added surface is worth it.
3. **Tool shape: new `portal_search` vs. a `portal` source under `public_records`.** A
   portal *is* a public-records source. Folding it into `public_records(sources=["portal"])`
   reuses one umbrella but muddies that tool's "keyless national source" identity and its
   fan-all-sources default. The plan proposes a **separate** `portal_search` (jurisdiction-
   scoped, row+detail-URL shaped); a reviewer may prefer the umbrella.
4. **Reader/solver escalation for JS-only portals.** v1 excludes it (Non-goals). If a
   priority state portal turns out JS-only, do we pull that escalation forward into P2?
5. **`kind` taxonomy.** `business` / `license` covers the two v1 portals; is that the right
   axis for future portals (court dockets, property records, voter files), or should the
   registry key on a richer capability descriptor from the start?

---

## Review outcomes (adversarial review, 2026-08-08 — incorporated)

An independent code-grounded review verified the plan's three load-bearing claims against
the source and returned **ship with changes**. Confirmed as correct (no change): the
citation-registry integration is genuinely zero-change to `deep_research.py`
(`ToolOutput.web_sources` → `loop.py` → `spawn.py` `_ChildResult` → `_collect_sources`);
the SSRF reuse via `_send_following_safe_redirects` (fetch.py:757) is the right per-hop
seam and is *stronger* than the public_records clients (nppes.py opens a bare client with
no SSRF guard); and the parent⊆child clamp grant (`JERV_TOOLS` + `RESEARCH_TOOLS`, so
`research`/`research_deep` get it and the library/reports personas don't) is right.

The following revisions are **binding on implementation**:

- **R1 (was HIGH-1) — DFS name→id→detail bridge is unverified and cross-system.** DICE
  (`dice.fldfs.com`) and `licenseesearch.fldfs.com` are different DFS apps; a DICE result
  row may not carry the `licenseesearch` numeric id that `/Licensee/<id>` needs. Before
  building the DICE POST path, check whether **licenseesearch.fldfs.com exposes its own
  name search** (same host as the detail page → no cross-host id mapping). Prefer that.
  Gate Wave P2 on a captured fixture that actually shows a results row carrying the id used
  in `/Licensee/<id>`. Do not assume the bridge.
- **R2 (was MED-2) — form detection must NOT reuse the challenge-page escalation.**
  `_is_challenge_page` raises → `fetch()` routes to `_recover` → reader → solver (70s). A
  search form IS the served content; a renderer never submits it, so escalation is pure
  latency waste on the common case. Short-circuit the form verdict WITHOUT recovery (a
  distinct error/result `fetch()` does not hand to `_recover`), not by reusing the
  challenge seam.
- **R3 (was MED-3) — detector precision.** Gate the "just a form" True verdict on the
  ABSENCE of a result-bearing table/list in the RAW HTML, not merely on extracted-body
  emptiness — a server-rendered results page can extract near-empty and be misflagged,
  re-creating the "no record" lie. Add a negative fixture: "search form + populated
  results table below." The reader/solver path has no raw HTML (markdown only), so specify
  its text-only fallback explicitly or rely on direct-path detection (with a fixture for
  whichever you choose).
- **R4 (was MED-4) — `fetch_html` must ride `_send_following_safe_redirects`.** A copy of
  the reader path's `client.get(url, follow_redirects=True)` would follow redirects inside
  httpx and skip per-hop host re-validation — the one way this becomes an SSRF primitive.
  P1's security test must assert refusal of a **mid-redirect private hop**, not only a
  private initial host.
- **R5 (was LOW-5) — delineate `portal_search` from public_records' `license` source.**
  public_records already has a national NPPES `license` source; sharpen both descriptions
  (national registry vs. state portal) and cross-reference, so tool selection stays clean.
- **R6 (was LOW-6/7/8) — repo hygiene folded into the waves.** (a) Index the doc: either
  move it to `docs/proposed/` (its status is Proposed and `docs/plans/README.md` says
  unscheduled ideas live there) or, when scheduled, give it an active status and add the
  `docs/plans/README.md` row — the landing PR must do one of these for the `docs` gate.
  (b) `portal_search.tool` must be paired to an always-registered handler (wire
  `build_portal_handlers` unconditionally in `main.py`, reporting "not configured" on empty
  config). (c) Any persona/tool prompt body change must bump its `version:` AND update the
  pinned digest in `test_agents.py::test_persona_prompts_pinned_to_their_versions`.

Additions the plan was **missing**:

- **Caching / rate-limit posture.** Government portals block more aggressively and carry
  clearer ToS exposure than the keyless APIs; give each resolver a `TTLCache` + polite
  throttle mirroring the public_records clients (nppes.py:80).
- **Post-deploy live smoke.** Faked fixtures cannot catch silent portal drift (a parser
  that drifts just returns `ok=False`). Add a one-shot debug route (mirroring
  `POST /api/debug/fetch`, scope `web.fetch`) to run a real resolver once after deploy for
  each v1 adapter.

Open-question decisions (from the review):

1. **Wave order → ship P3 first, renumbered** (dependency-free, the actual lie-stopper,
   helps every portal) — but only after R2 + R3 land, else it trades a silent failure for a
   slow one that can hide real results.
2. **`fetch_html` → yes, add it** on `_send_following_safe_redirects` (R4). `fetch().links`
   is lossy — `_collect_links` dedupes, drops order, caps at 40 — so raw HTML is needed for
   row↔detail-URL association.
3. **Tool shape → separate `portal_search`** (jurisdiction+kind dispatch doesn't fit
   public_records' fan-all-national default), with R5's collision fix.
4. **Reader/solver escalation → keep a non-goal, decide per-adapter.** If the same-host
   licenseesearch search (R1) turns out JS-only, opt that ONE adapter into the solver tier
   from the P2 feasibility check — not a v1 policy.
5. **`kind` taxonomy → ship `business`/`license` as simple open strings.** Adding a kind is
   a one-file change; dispatch on `(jurisdiction, kind)` and advertise available pairs from
   the registry. Revisit only when a third portal strains the axis.
