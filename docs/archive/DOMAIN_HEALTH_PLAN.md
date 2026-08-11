# Blocked Domains — 24h Paywall/Bot-Wall Skip

> **Status:** Shipped 2026-08 · migration 0163 (`app.blocked_domains`) · **Superseded-by:** —

A global, self-extending skip list so `web_fetch`/`web_search` stop wasting calls
on a site that just proved unreadable. When the fetcher hits a **persistent hard
block** on a domain — a paywall/subscriber wall, a bot-challenge interstitial, or
an unreadable JS shell — the **domain** is recorded for 24 hours; later searches
drop results from listed domains (and tell the agent how many were hidden), and
later fetches of a listed domain short-circuit without a network call.

## Why

Before this, a paywalled/bot-walled page failed silently and the model would keep
re-fetching the same wall (and search kept surfacing it) across turns, burning the
turn budget on a site that cannot be read. The per-turn `failed_fetches` memo only
covered the *same URL* within *one* turn; this extends the idea to the **domain**,
**globally**, for a day.

## Detection (what counts as a block)

Only a PERSISTENT block is recorded — precision over recall, since a false
positive would blocklist a real site for a day:

- **Paywall** — `web/fetch.py::_is_paywall_page`: a SHORT extracted body carrying a
  canonical subscribe-wall phrase (`subscribe to continue reading`,
  `this content is for subscribers`, …). Length-gated like `_is_challenge_page`, so
  a long article that merely mentions "subscribe" is never flagged. `_fetch_direct`
  raises `WebFetchError(transient=False)`. Reason → `paywalled` (also status 401/402).
- **Bot-wall** — the existing `_is_challenge_page` (Cloudflare/DataDome/etc.), plus
  status 403/429. Reason → `bot_blocked`.
- **Unreadable shell** — reserved in the schema (`unreadable`), NOT auto-recorded
  yet: an empty JS shell comes back as a normal (empty) result string, not a
  `WebFetchError`, so classifying it at the handler risks false positives (a legit
  empty page, paging past the end). Left for a follow-up.

**Never recorded:** a definitive 404/410 (the page is gone, not the site), a
transient timeout/DNS/5xx (`WebFetchError.transient=True`), or a `SearchFormError`
(the query never ran). `WebFetchError` grew a `transient` flag that the fetcher sets
at the HTTP-error raise sites (no status or ≥500 ⇒ transient) and the DNS-miss raise.

## Record → store → expire

- `web/domain_health.py::DomainSkipRepo` (constructed `DomainSkipRepo(maker)`) is the
  read/write seam, running under `queue.SYSTEM_CTX` (owner-kind) since this is global
  SYSTEM reference data, not owner-domain data.
- `record(host, reason, url)` upserts `app.blocked_domains` (migration 0163):
  `ON CONFLICT (host)` bumps `hit_count`, resets the 24h window, updates reason +
  last URL. Host normalized with `web/favicon.py::normalize_host` (bare lowercase).
  **Best-effort** — swallows/logs any error, never raises into the fetch handler.
- `active_hosts()` reads `WHERE expires_at > now()` (lazy expiry is the source of
  truth — no sweep). **Fail-open** — any DB error yields an empty set, so a DB blip
  never blocks a search or fetch.

## Table (`app.blocked_domains`, migration 0163)

`host text PK`, `reason text CHECK (paywalled|bot_blocked|unreadable)`,
`observed_at`, `expires_at DEFAULT now()+24h`, `hit_count`, `last_url`; index on
`expires_at`. RLS mirrors `app.canonical_predicates` (0031): global read
(`USING (true)`), owner/system write (`app.is_owner()`), granted to `jbrain_app`.

## Tool seams (`agent/webtools.py`)

`build_web_handlers(..., domain_skips=DomainSkipRepo|None)` (wired in `main.py`):

- **web_fetch** — before the network call, short-circuit a host in `active_hosts()`
  with a clear "skipped for the next day; web_search elsewhere" message. After a
  `WebFetchError`, `_record_block` records iff `_block_reason` classifies it as a
  persistent block (paywall/bot-wall) — never a transient/404/search-form. The
  per-turn `failed_fetches` memo is unchanged.
- **web_search** — drop hits whose host is in `active_hosts()` and append
  "(N result(s) hidden as known-paywalled or inaccessible.)".

## Tests

- `tests/integration/test_blocked_domains_rls.py` — RLS proof (every scope reads;
  scoped INSERT raises, scoped UPDATE affects 0 rows; owner/SYSTEM upsert), plus a
  `DomainSkipRepo` round-trip (upsert bumps hit_count, lazy expiry drops stale rows).
- `tests/unit/test_web.py` — `_is_paywall_page` precision, paywall raises
  non-transient, timeout/5xx set `transient`, 404 stays non-recordable,
  `_block_reason` classification, and webtools seams (search drops + counts, fetch
  short-circuits, fetch records a paywall but not a 404) via a fake repo (no DB).

## Follow-ups

- Auto-record the `unreadable` empty-shell terminal once it can be classified
  without false positives (it is a normal empty result today, not an error).
- Consider running `_is_paywall_page` on the reader/solver recovery path too (today
  it only runs on the direct fetch, matching where the wall is served).
