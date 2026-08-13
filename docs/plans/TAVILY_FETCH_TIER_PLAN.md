# Tavily Fetch Tier — a hosted recovery tier for walled web_fetch

> **Status:** In progress · **Last verified:** 2026-08-12 · **Waves:** T1✅ T2✅ T3✅ T4◻️

`web_fetch`'s hardest misses are pages behind a managed bot wall (Cloudflare
Turnstile, DataDome), a metered paywall, or a JS shell our static extractor can't
see. The stack already escalates **direct → reader → byparr solver**
(`CHALLENGE_SOLVER_PLAN.md`), but the two recovery tiers are **on-box browser
sidecars** — the `reader` (headless Chromium) and `byparr` (stealth browser) — and
they are exactly the fragile, host-dependent moving parts the owner **cannot debug
remotely** (`CLAUDE.md` non-negotiable #10: no terminal). When byparr lags
Cloudflare's latest challenge (its own plan calls it best-effort, `S2`), a walled
source is simply lost, and the owner has no shell to diagnose the sidecar.

[Tavily](https://tavily.com) offers a hosted **Extract API** that renders and
un-walls a URL on *their* infrastructure and returns clean content. Wired as a
**fourth recovery tier**, it catches pages the on-box tiers miss without adding a
sidecar to maintain — a hosted safety net that fires only when the free on-box path
is exhausted, so cost + off-box exposure stay **bounded to already-blocked URLs**.

And the box **learns**: when byparr genuinely fails on a domain but Tavily then
recovers it, that domain is recorded, so future fetches to it **skip straight to
Tavily** instead of burning the doomed direct→reader→byparr legs on every visit. The
learned list rides the existing per-domain fetch-health store, with the same 24h TTL
so a site that later drops its wall silently returns to the free path.

This is a **single-owner box**, so the tier **ships enabled** and is operated
**entirely from the PWA** — the owner pastes their Tavily API key into **Settings**
and flips a manual **on/off toggle**, with **no terminal step** (non-negotiable #10).
The env var is only a fallback for the key; the live control surface is the GUI.

This plan is **Extract-only**. Tavily also sells a Search API, but SearXNG
(`web/search.py`) stays the sole search backend — it is free, on-box, and carries
infoboxes / instant answers / news+science categories Tavily's search does not
(scoped out in "Non-goals").

## Design — a fourth fetch tier, owner-controlled from Settings

### The tier

`WebFetcher._recover` (`backend/src/jbrain/web/fetch.py:763`) already walks a tuple
of tiers, each a no-op miss (`None`) when it can't help, holding a thin result while
escalation continues. Tavily becomes one more entry:

1. **direct** — browser-headers httpx GET (unchanged).
2. **reader** — on-box headless Chromium for a JS shell / soft wall (unchanged).
3. **solver** — on-box byparr stealth browser for a JS/managed challenge (unchanged).
4. **tavily** — `_fetch_via_tavily`: `POST {tavily_url}/extract` with the target URL,
   rendering + un-walling on Tavily's cloud; the returned content is extracted and
   windowed **exactly like the reader path** (`_window_and_find`), so pagination /
   `find` / `outline` all work.

**`_fetch_via_tavily` (new)** mirrors `_fetch_via_reader` (`fetch.py:1073`): the
endpoint is owner-pinned, never model-supplied; `guard_public_host` runs on the
target first (an internal hostname is never handed to Tavily); it returns `None` when
disabled/unconfigured, on any Tavily error, or when the extracted content is *itself*
a challenge/paywall page (`_is_challenge_page` / `_is_paywall_page` run on Tavily's
output just as on the reader's), so a laundered "Just a moment…" never becomes a cited
`WebSource`. A `web.tavily_used` log (the `web.solver_used` analog) makes the tier
diagnosable.

**Base tier order — tavily LAST** (direct → reader → solver → tavily): the on-box free
tiers run first, so on a *first* visit to a walled domain Tavily is reached only when
everything on-box fails. The learned routing below is what makes *repeat* visits cheap.

### Learned Tavily-first routing (byparr fails → prefer Tavily next time)

A domain byparr can't clear is almost always *persistently* hard (the wall is a
fingerprint/JS-management the stealth browser structurally can't defeat, not a fluke),
so paying the full direct→reader→byparr escalation on *every* future fetch to it is
pure waste. When the on-box stack fails but Tavily saves the fetch, the box records the
domain and routes it **Tavily-first** thereafter — the *learned* form of the static
`solver_first_domains` shortcut (`config.py:181`), which it supersedes for this purpose.

This rides the existing **per-domain fetch-health store** — `app.blocked_domains`
(migration 0163) via `DomainSkipRepo` (`web/domain_health.py`), which already records a
domain with a `reason` + a 24h lazy-TTL and is read to route fetches:

- **Record (the precise trigger):** in `_recover`, when the **solver tier genuinely
  ran and missed** (byparr returned a still-challenged / empty page — *not* a byparr
  transport outage, which is transient and never recorded, mirroring the existing
  `transient` discipline) **and a later tier (Tavily) then recovered the page**, record
  the host with a new reason **`solver_failed`**. Recording only on *byparr-miss-then-
  Tavily-success* is deliberate: it means "the on-box stack can't do this domain but
  Tavily can" — exactly the domains worth routing to Tavily. A domain where *both* fail
  is a hard block the existing `_record_block` path skips (24h), not a Tavily-first lead.
- **Route:** the fetcher consults the live `solver_failed` host set (a new
  `tavily_first_hosts()` reader, the `_prefers_solver` analog) and, **when Tavily is
  enabled + keyed**, sends a listed host **straight to Tavily**, skipping
  direct→reader→byparr. A Tavily miss still falls through to the normal path (so a
  learned entry degrades, never hard-fails), exactly as `solver_first` does today.
- **TTL re-probe:** the 24h expiry means a listed domain periodically re-tries the free
  on-box path; if byparr still fails it re-records, if the site dropped its wall it
  silently returns to free fetching — no permanent Tavily dependence, no sweep needed.
- **Inert without Tavily:** with Tavily off/keyless there is nowhere better to route, so
  a `solver_failed` host just runs the normal path (byparr fails again, harmlessly). The
  learned list only *does* anything when Tavily is live.
- **Store seam:** `solver_failed` is a **reroute**, not a skip, so it is **excluded from
  `active_hosts()`** (the short-circuit skip set) and surfaced only via the new
  `tavily_first_hosts()` reader. `VALID_REASONS` + the table's `reason` CHECK gain
  `solver_failed` (a migration). The fetcher stays DB-free: it holds two thin injected
  best-effort async callbacks (a `tavily_first` host lookup + a solver-miss recorder),
  both backed by `DomainSkipRepo` from `main.py` — the same shape as the settings
  provider below, never a session in the egress object.

### Runtime control — key + toggle in Settings, read live (the Gmail precedent)

The API key is a **secret set from the GUI**, and the on/off is a **manual toggle** —
so both live in the `app.settings` store (migration 0012), read live per fetch, not
in static env config. This follows the **Gmail-credentials pattern** exactly
(`settings_store.py:101-107`, 403-427): a third-party secret stored via the Settings
panel, **taking precedence over a `JBRAIN_*` env fallback**, changeable with no
restart.

- **`tavily_api_key`** (`app.settings`) — the secret, written from the Settings panel;
  **takes precedence over the `JBRAIN_TAVILY_API_KEY` env fallback**. Never echoed back
  on read (the GET reports only whether a key is *set*, masked — the Gmail secret is
  likewise never returned).
- **`tavily_enabled`** (`app.settings`) — the manual service toggle. **Default ON**
  ("enable right off the bat"), so the moment the owner pastes a key the tier is live
  with no second step; flip it OFF to disable the tier instantly while keeping the key.
- **Effective firing condition:** `tavily_enabled AND key present (stored-or-env)`. So
  a fresh box has the tier present and enabled but **inert until a key is entered** —
  the three-tier behaviour is byte-unchanged until then.

**Live read into the singleton fetcher.** `WebFetcher` is an app-lifetime singleton
built from static config (`main.py:450`), but `tavily_enabled`/`tavily_api_key` are
runtime DB settings. So the fetcher takes a **settings provider** — an injected
`async () -> (enabled, api_key)` that reads the store live under the owner context —
and `_fetch_via_tavily` consults it per fetch. This is the same "read `app.settings`
live per call" seam the LLM router uses for `llm_task_overrides`
(`settings_store.py:515`, `router._resolve_live`), so a toggle flip or a pasted key
takes effect on the **next fetch, no redeploy**.

**Non-secret config stays in `config.py`** (pinned, never model-supplied):
`tavily_url: str = "https://api.tavily.com"` and
`tavily_extract_depth: str = "advanced"` (Tavily's `basic`/`advanced` depth — advanced
un-walls harder at ~2× credits).

### The Settings panel + a no-terminal "Test"

`POST /api/debug/fetch` (`web.fetch` scope, from `CHALLENGE_SOLVER`) gains a
`tier="tavily"` selector that forces **only** `_fetch_via_tavily` (mirroring
`WebFetcher.solve()`'s byparr-isolation entry, `fetch.py:665`). The Settings panel's
**"Test key"** button calls it, so the owner verifies a freshly pasted key against a
real walled URL **from the PWA, with no terminal** — closing the loop non-negotiable
#10 demands.

### GUI gate (PROCESS.md)

Adding the Tavily key field + enable toggle (+ Test button) to `SettingsScreen.tsx` is
a **GUI surface change**, so it goes through the **GUI gate**: three interactive mock
HTML artifacts of the panel (modeled on the existing Gmail-credentials panel — a
masked secret field + a toggle), presented for the owner to choose the path *before*
implementation; the chosen mock lands in `docs/mocks/` as the binding spec. This is
the T2 critical-decision interruption.

**Rollout.** No compose/sidecar change — Tavily is a hosted API, not a container. The
api, rebuilt from this tree, carries the tier (enabled) and the Settings panel; the
owner pastes the key and it works. A stock box with no key is byte-unchanged.

## Waves

- **T1 ✅** — the tier + live runtime control, headless, fully covered offline. Adds
  `_fetch_via_tavily` wired **last** into `_recover`; the `tavily_enabled` /
  `tavily_api_key` settings-store keys + getters (Gmail precedent, env fallback for the
  key, default enabled); the settings-provider seam threading them live into
  `WebFetcher` (`main.py`); the `_is_challenge_page` / `_is_paywall_page` guards on the
  output; `tavily_url` + `tavily_extract_depth` config; the `POST /api/debug/fetch`
  `tier="tavily"` selector. Full unit coverage (`test_web.py` — Tavily faked via
  `httpx.MockTransport`: happy extract, disabled → no-op, no key → no-op, Tavily error →
  `None`, challenge/paywall body → `None`, the live-provider precedence, `_recover`
  four-tier order; `test_debug_api.py` — the selector; `test_settings_api.py` /
  store tests — the new keys). No new runtime dependency (reuses `httpx`). Docs → **In
  progress** (T1✅). Zero GUI in this wave — the tier is functional from env/DB alone.
- **T2 ✅** — learned Tavily-first routing (byparr fails → prefer Tavily), headless.
  Migration extends `app.blocked_domains`'s `reason` CHECK + `VALID_REASONS` with
  `solver_failed`; `domain_health.py` gains the reason-aware record + a
  `tavily_first_hosts()` reader and **excludes `solver_failed` from `active_hosts()`**;
  `fetch.py` records the host on a genuine byparr-miss-then-Tavily-success in `_recover`
  (never on a transient byparr outage) and routes a listed host Tavily-first (gated on
  Tavily enabled + keyed), via the two thin injected callbacks; `main.py` wires them
  from `DomainSkipRepo`. Full unit coverage (the record trigger incl. the
  transient-outage exclusion, the Tavily-first route + fall-through, the `active_hosts`
  exclusion); the `reason`-CHECK change gets its own test, and the existing
  `app.blocked_domains` RLS isolation test covers the reused table. Depends on T1 (needs
  the tier to route to).
- **T3 ✅** — the Settings GUI (the owner control surface). **GUI gate:** three
  interactive mocks in `docs/mocks/tavily-settings/` (A inline / B status-pill+switch /
  C progressive); the owner chose **B** — the panel ships a status pill + iOS-style enable
  switch + a combined "Save & test" (the binding spec). Ships a **dedicated**
  `api/tavily_settings.py` (`GET`/`PUT /settings/tavily` + `POST /settings/tavily/test`)
  rather than the generic `/settings`, so the secret is **never echoed** (status returns
  only `enabled`/`key_set`/`wired`/`effective`) and the "Test key" probe runs the live
  tier under the owner session (no debug token); the `SettingsScreen.tsx` panel (masked
  key field, On/Off toggle, Save/Test/Clear); the `TavilySettings` api-client types. Full
  frontend + backend coverage (`test_tavily_settings_api.py`, `SettingsScreen.test.tsx`).
- **T4 ◻️** — live validation + tuning, then archive. Against a known byparr-miss URL,
  confirm Tavily recovers real content (`web.tavily_used`), that a Tavily-laundered
  challenge is still an honest block, and that the domain lands on the learned
  Tavily-first list and short-cuts on the next fetch. Tune `extract_depth` + the request
  timeout against real extracts, and confirm the 24h re-probe. Fold the outcome into the
  config/settings comments + this plan, then archive.
  - **Post-deploy fix (2026-08-12):** the first live "Test key" 401'd — the Extract API
    rejects a body `api_key`; the key must ride the `Authorization: Bearer` header (and
    `urls` is a list). Fixed in `_fetch_via_tavily` with a request-shape regression test.
    Also dropped the panel's `.seg-primary` tint on "Save & test" so the action buttons
    match every other Settings row (plain `.seg`), not a selected-toggle segment.
  - **Verbose "Test key" (2026-08-13):** a rejected key read as a vague "no page came back"
    (which made a bad key look like a code bug). `WebFetcher.tavily_probe` now names each
    failure mode — a 401/403 **key rejection** (with the fix hint), a 429 rate-limit, an
    unreachable Tavily, an empty/walled page, or the tier off/keyless — and the owner-gated
    `/settings/tavily/test` route delegates to it. The fetch tier is unchanged (both share a
    new `_tavily_extract` so the request shape lives in one place).
  - **Timeout + repeat-extract cache (2026-08-13):** a live run showed
    `web.tavily_failed: ReadTimeout` — the shared 20s cap aborted an advanced extract mid-render.
    Gave Tavily its own `_TAVILY_TIMEOUT = 45s` (below the solver's 70s). Also added a 60-min
    in-process TTL cache keyed by URL (the SearXNG-cache pattern) so a research fan's repeat opens
    of the same walled URL collapse to ONE paid call; a block result is never cached (a transient
    wall must stay retryable), and the diagnostic probe always calls live.

## Non-goals (scoped out)

- **Replacing SearXNG.** Search stays on-box and free; Tavily Search's per-credit cost
  under deep-research fan-out (the reason `web/search.py` carries a TTL cache) and the
  loss of infoboxes / instant-answers / news+science make it a poor trade. Rejected.
- **Retiring the reader / byparr sidecars.** Tempting for the no-terminal owner, but
  premature — byparr may still beat Tavily on the hardest managed challenges. Revisit
  only after T3 shows Tavily's tier hit-rate. Until then Tavily is *additive*.
- **Ingesting Tavily's own answer/summary.** Only the extracted page content is used,
  windowed like any fetch; no synthesized summary is cited as a source.

## Security & posture

Same trust model as the reader/solver: owner-pinned base URL, SSRF-guarded **public**
target, reached only by `jerv` (KB-blind — no owner data in context, so what leaves is
a research URL, not personal facts). The genuine posture change from the on-box tiers:
Tavily is a **paid third-party cloud egress**, so a named vendor receives a
consolidated log of the URLs jerv could not otherwise read, tied to the owner's
account. That exposure **begins only once the owner enters a key** (a deliberate GUI
act, not a silent default), is **bounded to already-blocked URLs** (the tier fires only
after the on-box stack is exhausted), and the **Settings toggle is the instant,
no-terminal off switch**. The stored key is never echoed back on read; the
challenge/paywall guards run on Tavily's output exactly as on every other tier, so the
hosted tier can never launder junk into a citation either.
