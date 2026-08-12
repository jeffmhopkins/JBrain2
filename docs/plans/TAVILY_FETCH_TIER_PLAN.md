# Tavily Fetch Tier — a hosted recovery tier for walled web_fetch

> **Status:** Scheduled · **Last verified:** 2026-08-12 · **Waves:** T1◻️ T2◻️

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
is exhausted, so both **cost and off-box exposure stay bounded to already-blocked
URLs**.

This plan is **Extract-only**. Tavily also sells a Search API, but SearXNG
(`web/search.py`) stays the sole search backend — it is free, on-box, and carries
infoboxes / instant answers / news+science categories Tavily's search does not.
Superseding search was scoped out and rejected (see "Non-goals").

## Design — a fourth fetch tier, same trust seam as the reader

`WebFetcher._recover` (`backend/src/jbrain/web/fetch.py:763`) already walks a tuple
of tiers, each a no-op miss (`None`) when it can't help, holding a thin result while
escalation continues. Tavily becomes one more entry in that tuple:

1. **direct** — browser-headers httpx GET (unchanged).
2. **reader** — on-box headless Chromium for a JS shell / soft wall (unchanged).
3. **solver** — on-box byparr stealth browser for a JS/managed challenge (unchanged).
4. **tavily** — `_fetch_via_tavily`: `POST {tavily_url}/extract` with the target URL,
   rendering + un-walling on Tavily's cloud; the returned content is extracted and
   windowed **exactly like the reader path** (`_window_and_find`), so pagination /
   `find` / `outline` all work.

**`_fetch_via_tavily` (new).** Mirrors `_fetch_via_reader`
(`fetch.py:1073`) precisely:
- The endpoint is **owner-pinned config, never model-supplied**; only the public
  target URL (and the api key, in the request body) travel. `guard_public_host` runs
  on the target first — same as the reader/solver seams — so an internal hostname is
  never handed to Tavily.
- Returns `None` when unconfigured (no api key), on any Tavily error, or when the
  extracted content is **itself a challenge/paywall page** (`_is_challenge_page` /
  `_is_paywall_page` run on Tavily's output just as they do on the reader's), so a
  laundered "Just a moment…" never becomes a cited `WebSource`. `_is_search_form_page`
  is a no-op on the markdown path (no raw HTML), identical to the reader.
- Content flows through `_window_and_find`, so a Tavily-recovered page pages and
  keyword-jumps like any other.

**Config (`backend/src/jbrain/config.py`).** Three fields, following the connector
pattern already established by `anthropic_api_key` / `courtlistener_token`:
- `tavily_api_key: str = ""` — the master switch. **Empty disables the tier** and is
  the **default** — unlike the reader/solver (which default-on to on-box URLs), Tavily
  is **opt-in**, because it is a paid third-party egress. No key ⇒ the tier is a no-op
  `None` and the existing three-tier behaviour is byte-unchanged.
- `tavily_url: str = "https://api.tavily.com"` — pinned base URL, never model-supplied
  (like every other connector base).
- `tavily_extract_depth: str = "advanced"` — Tavily's `basic`/`advanced` extract depth;
  `advanced` renders more aggressively (higher un-wall rate, ~2× credits). Pinned, not
  model-supplied.

**Tier order — the one open decision (resolve in T2 against live data).** The default
this plan ships is **tavily LAST** (direct → reader → solver → tavily): the on-box
free tiers run first, so Tavily is billed only when *everything on-box* fails —
tightest cost + exposure. The alternative — tavily **before** byparr — exercises the
fragile stealth-browser sidecar less (an operational win for the no-terminal owner)
at the cost of paying Tavily before a solve that might have succeeded free. A
`tavily_first_domains` tuple (the `solver_first_domains` analog, `config.py:181`)
could later route known-hard hosts straight to Tavily; **deferred to T2**, gated on
whether live hit-rates justify it.

**Debug route.** `POST /api/debug/fetch` (`web.fetch` scope, from `CHALLENGE_SOLVER`)
already runs a URL through the real escalation. Add a `tier="tavily"` selector that
forces **only** `_fetch_via_tavily` — mirroring the existing `WebFetcher.solve()`
byparr-isolation entry point (`fetch.py:665`) — so the owner can verify the live
Tavily path from the debug console after enabling the key, with **no terminal**.

**Rollout.** No compose/sidecar change — Tavily is a hosted API, not a container.
The api, rebuilt from this tree, carries the new (disabled-by-default) tier; the owner
enables it by setting `JBRAIN_TAVILY_API_KEY` via the sanctioned env path. Because the
default is empty, a stock deploy that never sets the key is **completely unaffected**.

## Waves

- **T1 ◻️** — the tier + config + debug selector, fully covered offline. Adds
  `_fetch_via_tavily` wired **last** into `_recover`; the three `tavily_*` config
  fields; the `_is_challenge_page` / `_is_paywall_page` guards on Tavily's output; the
  `POST /api/debug/fetch` `tier="tavily"` selector. Full unit coverage in
  `test_web.py` (Tavily faked via `httpx.MockTransport`: a happy extract, an
  unconfigured no-op, a Tavily error → `None`, a challenge/paywall body → `None`, the
  `_recover` order with all four tiers) and `test_debug_api.py` (the new selector),
  web calls faked — no network in tests. `dev-setup.sh` documents the optional key.
  Docs reconciled: this plan → **In progress** (T1✅), the ROADMAP entry + this
  section ticked. Zero new runtime dependency (reuses `httpx`).
- **T2 ◻️** — live validation + tier-order decision after the owner sets the key.
  Against a known byparr-miss URL, confirm Tavily recovers real content (via a
  `web.tavily_used` log, the `web.solver_used` analog) and that a Tavily-laundered
  challenge is still an honest block. Decide tier order (last vs. before-byparr) from
  observed on-box solve-vs-Tavily hit-rates + latency, and whether a
  `tavily_first_domains` shortcut earns its keep. Tune `extract_depth` and the request
  timeout against real extracts. Fold the outcome into the config comments + this plan,
  then archive.

## Non-goals (scoped out)

- **Replacing SearXNG.** Search stays on-box and free; Tavily Search's per-credit
  cost under deep-research fan-out (the reason `web/search.py` carries a TTL cache) and
  the loss of infoboxes/instant-answers/news+science make it a poor trade. Rejected.
- **Retiring the reader / byparr sidecars.** Tempting for the no-terminal owner, but
  premature — byparr may still beat Tavily on the hardest managed challenges. Revisit
  only after T2 shows Tavily's tier hit-rate. Until then Tavily is *additive*.
- **Ingesting Tavily's own answer/summary.** Only the extracted page content is used,
  windowed like any fetch; no synthesized summary is cited as a source.

## Security & posture

Same trust model as the reader/solver: owner-pinned base URL, SSRF-guarded **public**
target, reached only by `jerv` (KB-blind — no owner data in context, so what leaves is
a research URL, not personal facts). The genuine posture change from the on-box tiers:
Tavily is a **paid third-party cloud egress**, so a named vendor receives a
consolidated log of the URLs jerv could not otherwise read, tied to the owner's
account. That exposure is **bounded to already-blocked URLs** (the tier fires only
after the on-box stack is exhausted) and is **off by default** (empty api key). The
challenge/paywall guards run on Tavily's output exactly as on every other tier, so the
hosted tier can never launder junk into a citation either.
