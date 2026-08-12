# Tavily Fetch Tier — a hosted recovery tier for walled web_fetch

> **Status:** Scheduled · **Last verified:** 2026-08-12 · **Waves:** T1◻️ T2◻️ T3◻️

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

**Tier order — decided in T3 against live data.** The default this plan ships is
**tavily LAST** (direct → reader → solver → tavily): the on-box free tiers run first,
so Tavily is billed only when *everything on-box* fails. The alternative — tavily
*before* byparr — exercises the fragile sidecar less (an operational win for the
no-terminal owner) at the cost of paying Tavily before a solve that might have
succeeded free. A `tavily_first_domains` shortcut (the `solver_first_domains` analog,
`config.py:181`) is **deferred to T3**, gated on live hit-rates.

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

- **T1 ◻️** — the tier + live runtime control, headless, fully covered offline. Adds
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
- **T2 ◻️** — the Settings GUI (the owner control surface). **GUI gate first**: three
  mocks of the Tavily panel → owner picks → binding spec in `docs/mocks/`. Then: the
  key-write endpoint (secret stored, **never echoed** — Gmail precedent) + the
  `tavily_enabled` toggle in the `/settings` GET/PUT (`SettingsOut`/`SettingsPatch`) +
  store setters; the `SettingsScreen.tsx` panel (masked key field, on/off toggle,
  "Test key" button hitting the `tier="tavily"` debug route). Frontend + backend unit
  coverage (`SettingsScreen.test.tsx`, `test_settings_api.py`).
- **T3 ◻️** — live validation + tuning, then archive. Against a known byparr-miss URL,
  confirm Tavily recovers real content (`web.tavily_used`) and that a Tavily-laundered
  challenge is still an honest block. Decide tier order (last vs. before-byparr) from
  observed on-box solve-vs-Tavily hit-rate + latency, and whether `tavily_first_domains`
  earns its keep. Tune `extract_depth` + the request timeout against real extracts. Fold
  the outcome into the config/settings comments + this plan, then archive.

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
