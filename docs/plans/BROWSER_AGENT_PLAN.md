# Browser agent — a real browser jerv can drive, and search off Tavily

> **Status:** Proposed · **Last verified:** 2026-10-04 · **Waves:** B0◻️ B1◻️ B2◻️ B3◻️

`web_fetch` reads pages; it cannot *use* them. Epic Theatres is the case that started this
(2026-10-04): every page reads "Please select a location" until a visitor picks a theater,
because the site keeps that choice in browser storage. Direct fetch, the reader and Byparr
all return the same 221-character template, and jerv spent 48 steps on aggregators before
giving up. A browser that can click "Titusville" and read the result does it in about five.

The prior-art survey is `docs/research/browser-agent/SELF_HOSTED_BROWSER_AGENT_OPTIONS.md`.
Its conclusion, which this plan adopts: **don't build a browser agent — adopt a browser
tool server and run our own small loop over it.**

## 1. Decisions

| Question | Decision | Why |
|---|---|---|
| Browser layer | **Microsoft's `playwright-mcp`**, official image `mcr.microsoft.com/playwright/mcp`, pinned by tag, HTTP transport | Apache-2.0, actively maintained, bundles its own Chromium. Observes pages as a pruned accessibility tree with element refs, so each step is one small tool call (`browser_click ref=12`) — no pixel coordinates, no large JSON schema. That is the shape a ~6B-active model can do. |
| Who runs the loop | **Our backend**, through the LLM adapter (non-negotiable 1) | browser-use and Stagehand own their model loop, bypass the adapter, and their own docs/maintainers say small Qwen models fail their schemas. playwright-mcp has no model of its own — the loop is ours, logged in the run-log. |
| Where browsing happens | **A sub-agent with its own context** (owner decision 2026-10-04) | jerv calls `browse(goal, start_url?)` and gets back a short text answer with source URLs. Page snapshots never enter jerv's context. |
| Runner-up | Vercel `agent-browser` (Apache-2.0) | Richer built-in policies (action allowlists, confirmations, delta snapshots); no confirmed official image. Measured against playwright-mcp in B1's bake-off. |
| Stealth | Stock Chromium first | A 2026 benchmark across 31 Cloudflare sites from a residential IP: vanilla Playwright cleared 24, the best stealth tool 28. A theater picker needs JavaScript, not evasion. Byparr cannot be driven (FlareSolverr API, not CDP); if stealth matters later, a zendriver-launched Chrome over CDP is the upgrade path. |
| Search | **SearXNG primary**, metered APIs as backstop, Tavily optional | The box ran a pre-2026-09-04 SearXNG; the pinned build's curl_cffi client fixed this exact block pattern elsewhere. See B3. |

## 2. The fence — security is the network, not the tool flags

Playwright's own docs call its origin allow/block lists "not a security boundary"; a
playwright-mcp setup has been steered into screenshotting a cloud metadata endpoint.
Injection into browsing agents succeeded 42–68% of the time in 2026 tests, worse on weaker
models. So the rules are structural:

- **Own network.** The browser joins a new `browser` network, which only the api and an
  egress proxy also join. It never touches `internal`, where `db`, the supervisor and the
  model servers live.
- **Egress proxy that resolves DNS itself** (so DNS rebinding can't slip past) and denies
  RFC1918, loopback, link-local incl. 169.254/16, CGNAT 100.64/10, IPv6 ULA and link-local,
  and every compose service name.
- **Rule of Two.** The sub-agent sees untrusted web content and nothing else: no notes, wiki
  or firewalled domains, no credentials, an `--isolated` in-memory profile.
- **Quarantined result.** Its answer returns to jerv as data — text plus source URLs, with
  links and images stripped — and can never pick a side-effecting tool.
- **Read-only actions.** Navigate, snapshot, click, select, type into search/filter fields,
  scroll, back, wait, an on-demand screenshot. Refused: logins, checkout and payment,
  downloads, uploads, and any form submit outside a search/filter. Enforced in the host
  loop's action allowlist, not the prompt.
- **Budgets.** ~20 steps, a wall-clock cap, a page cap, loop detection. Success is checked
  against the final page, never taken from the model's "done".
- **Existing sidecars.** `reader` and `byparr` also take untrusted pages but sit on
  `internal` beside the database. They move behind the same fence in B0.

## 3. Waves

### B0 — The fenced browser, and the instrument ◻️
- Compose: the `browser` service (playwright-mcp, pinned tag, `--isolated`, HTTP port,
  `shm_size`, `mem_limit`), the `browser` network, the egress proxy service. Move `reader`
  and `byparr` behind the proxy too.
- A compose test asserting the network membership (like the pysandbox one): the browser
  and the web sidecars cannot reach `internal`.
- Debug route `POST /api/debug/browse` `{goal, start_url?, max_steps?}` + `debug-connect.sh
  browse`: runs one goal and returns the step trace (action, snapshot tokens, latency), the
  answer and the final URL. Async with job polling (a run outlasts the tunnel's ~100 s).
- **On-box, with notice:** the fence tests must fail closed — 169.254.169.254, an RFC1918
  address, `db:5432`, `searxng:8080`, via a redirect and a rebinding hostname — plus an
  injection canary page. Then the Epic Theatres goal end to end.

### B1 — The `browse` sub-agent ◻️
- Host loop in the backend: the read-only tool allowlist mapped onto playwright-mcp's
  tools, snapshot pruning (interactive-only, delta where available), budgets, loop
  detection, final-page verification, the quarantined result. Its own task route and slot
  role (research-sized), so its prefix caches apart from jerv's.
- `browse` tool for jerv; jerv's prompt says when to reach for it (a page needs a choice
  made, a click, a tab, a form filter) and that its result is quoted data.
- `web_fetch` hands off: a thin page that reads as a location/store gate or a JS shell says
  so, and suggests `browse`, instead of leaving jerv to guess "JavaScript". The 200-char
  bar that let Epic's 221-char template through as a success is revisited here.
- **Bake-off** on 20–30 of the owner's real tasks, playwright-mcp vs agent-browser behind
  the same loop: success (verified on the final page), steps, time, snapshot tokens,
  tool-call parse failures. The winner ships; the numbers go in this plan.

### B2 — Seeing it in chat ◻️ (GUI gate: three mocks, owner picks)
- `browse` steps in the Worked pane as readable rows ("Opened epictheatres.com", "Picked
  'Titusville'", "Read today's showtimes") with a small screenshot per step.
- Settings: browsing on/off, step budget. Ops: the browser container's health and the last
  runs' traces.

### B3 — Search off Tavily ◻️
- **Done ahead of the plan (PR #1557):** SearXNG, reader and byparr pinned to dated/digest
  images — the update never re-pulled `latest`, so all three had frozen at install.
- SearXNG settings: enable Bing and the structured engines (Wikipedia, Wikidata, GitHub,
  StackExchange, arXiv); drop or down-weight DuckDuckGo (it bans the IP); shorten
  `suspended_times` (defaults bench an engine for a day on one captcha); surface engine
  suspensions on Ops.
- A rate-limited queue in front of SearXNG for deep-research bursts, on top of the
  existing one-hour cache.
- Provider chain with per-provider budgets: SearXNG → Brave Search API (about 1,000
  queries/month on its free credit, then $5/1,000) → optionally Mojeek (£2/1,000). Tavily
  becomes an optional last tier the owner can switch off.
- A week of per-engine health on the new pin (including a 50-query burst) decides whether
  Brave's free credit covers the overflow.

## 4. Open questions
1. **Owner confirmations.** v1 refuses every state-changing action. Should a later wave add
   a PWA confirmation card ("jerv wants to submit this form on X — allow?"), or keep the
   browser strictly read-only?
2. **A live view.** kernel-images or Steel (both Apache-2.0) could stream the browser into
   the PWA. Worth it after B2's per-step screenshots, or not at all?
3. **Brave API key.** One key with a free monthly credit, or SearXNG alone plus the browser
   as a last resort?
</content>
</invoke>
<invoke name="Bash">
<parameter name="command">cd /home/user/JBrain2 && mkdir -p docs/research/browser-agent && { printf '%s\n\n%s\n\n' "# Self-hosted browser agent options — prior-art survey" "> **Status:** Research · **Last verified:** 2026-10-04 · Feeds \`../../plans/BROWSER_AGENT_PLAN.md\`."; tail -n +3 "reports/Self hosted browser agent options.md" | sed '1s/^/## Summary\n\n/'; } > docs/research/browser-agent/SELF_HOSTED_BROWSER_AGENT_OPTIONS.md && head -8 docs/research/browser-agent/SELF_HOSTED_BROWSER_AGENT_OPTIONS.md | cut -c1-120