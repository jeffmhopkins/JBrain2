# Browser agent — a real browser jerv can drive, and search off Tavily

> **Status:** In progress · **Last verified:** 2026-10-05 · **Waves:** B0🟡(built; on-box fence tests + Epic run pending) B1🟡(built; bake-off pending) B2◻️ B3◻️

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
| Egress proxy | **Squid** (Canonical's `ubuntu/squid`, Ubuntu-LTS build, digest-pinned), built on the box with the deny list baked in and a build-time `squid -k parse` | Resolves DNS itself and checks the resolved address, so a public name pointing at a private address is refused. Smokescreen (Stripe) was the prior-art pick but publishes no official image; Squid's `dst` ACLs do the same job from a maintained distro package. Verified locally against the deny list (169.254/16, RFC1918, loopback, CGNAT, `db`, `searxng`, `localtest.me`, IPv4-mapped v6, CONNECT) — every one 403, a public site 200. |
| MCP client | **Hand-written over httpx** (`web/mcp_client.py`), not the `mcp` SDK | Four operations (open, list, call, close) against one server we run; the SDK would bring anyio/starlette/sse into the api for one caller. Handles JSON and SSE replies; verified against the real v0.0.82 image. |
| Model route | **`browse.step`**, its own task, following `agent.turn`'s model, pinned to the **research slot** | A new slot role would mean a ninth slot in the Flash-Next pool; the research slot is already the sub-agents' and keeps browse snapshots out of jerv's interactive prefix. Following agent.turn means a local box never sends page text to a cloud default. |
| Search | **SearXNG primary**, metered APIs as backstop, Tavily optional | The box ran a pre-2026-09-04 SearXNG; the pinned build's curl_cffi client fixed this exact block pattern elsewhere. See B3. |

## 2. The fence — security is the network, not the tool flags

Playwright's own docs call its origin allow/block lists "not a security boundary"; a
playwright-mcp setup has been steered into screenshotting a cloud metadata endpoint.
Injection into browsing agents succeeded 42–68% of the time in 2026 tests, worse on weaker
models. So the rules are structural:

- **Own network.** The browser joins a new `browser` network, which only the api and an
  egress proxy also join. It never touches `internal`, where `db`, the supervisor and the
  model servers live. **Accepted risk (B0/B1):** because the api is on `browser` to reach the
  MCP port, a *compromised* Chromium (it runs `--no-sandbox`) could open TCP to `api:8000`
  directly, past Caddy — the same shape `pysandbox` and `jcode` already have. A page cannot:
  every request goes through the proxy, which refuses private addresses. It takes a browser
  exploit first. B2 closes it (below).
- **Egress proxy that resolves DNS itself** (so DNS rebinding can't slip past) and denies
  RFC1918, loopback, link-local incl. 169.254/16, CGNAT 100.64/10, IPv6 ULA and link-local,
  and every compose service name.
- **Rule of Two.** The sub-agent sees untrusted web content and nothing else: no notes, wiki
  or firewalled domains, no credentials, an `--isolated` in-memory profile.
- **Quarantined result.** Its answer returns to jerv as data — text plus source URLs, with
  links and images stripped — and can never pick a side-effecting tool.
- **Tiered actions, not a blanket read-only** (owner decision 2026-10-04: form filling is
  allowed, behind approval). Every state-changing action passes the risk gate in §2a before
  it runs; the gate lives in the host loop, never in the prompt.
- **Budgets.** ~20 steps, a wall-clock cap, a page cap, loop detection. Success is checked
  against the final page, never taken from the model's "done".
- **Existing sidecars.** `reader` and `byparr` also take untrusted pages but sit on
  `internal` beside the database. Moving them behind the same fence is **deferred from B0**:
  each needs its own proxy plumbing verified (the reader's headless Chromium and byparr's
  Camoufox take a proxy differently, and byparr's FlareSolverr API passes one per request),
  and a mistake silently breaks two `web_fetch` tiers on a box the owner cannot reach by
  shell. It is a follow-up with its own on-box check, not a compose one-liner.

## 2a. The risk gate — mundane actions run, risky ones ask

The owner wants routine work done without a prompt for every click, and anything touching
personal data or commitments to wait for approval. Prior work converges on the same shape:
**deterministic rules first, a separate judge for the gray zone, and the person approves
with the evidence in front of them.**

Prior work this borrows from:
- **OpenAI Operator** — asks before "significant" actions (purchases, sending), and hands
  control to the person for passwords and payment ("takeover") rather than typing them.
- **Claude in Chrome / computer use** — per-site permissions, always-confirm categories
  (purchases, publishing, sharing personal data), and an injection classifier on page
  content.
- **Vercel `agent-browser`** — declarative action policies with confirmation prompts.
- **Microsoft Presidio** (MIT) — detects PII in text: names, emails, phones, addresses,
  card and account numbers, IDs. Used on every value the agent is about to type.
- **The page itself** — HTML `autocomplete` tokens (`cc-number`, `email`, `tel`,
  `street-address`, `current-password`, `one-time-code`), `type=password`, payment iframes,
  a form's method and the submit button's label are strong, cheap signals.
- **Meta LlamaFirewall's AlignmentCheck, Google's Conseca, CaMeL, Progent** — a separate
  check that the next action still serves the owner's original goal, judged without the
  page's text in its instructions (so an injected page can't argue for itself). This is
  also how this repo's own Claude Code auto-mode classifier works.

The gate, in order (first match wins):

| Tier | Examples | What happens |
|---|---|---|
| **Never** | typing a password, card number, CVV, bank/ID number, one-time code; downloads that execute | Refused. The card tells the owner to do that step themselves. |
| **Ask** | any submit carrying PII (Presidio hit or a PII `autocomplete` field), creating an account, sending or posting a message, booking, reserving, ordering, agreeing to terms, a value that came from the owner's own data | Paused. A PWA card shows the screenshot, the site, the exact fields and values to submit, and Allow / Deny / Always allow this on this site. |
| **Auto** | navigation, search and filter forms (GET, `role=search`), choosing a location, date or tab, dismissing cookie banners, a site the owner has said "always allow" for this action kind | Runs, and appears in the trace. |
| **Judge** | anything the rules don't place | A separate small model call sees the owner's goal, the pending action and the field labels — never the page's prose — and answers auto or ask. Unsure means ask. |

Owner choices ("always allow on this site") are stored per site and action kind, are
listed and revocable in Settings, and never extend to the Never tier.

## 3. Waves

### B0 — The fenced browser, and the instrument 🟡 (built 2026-10-05; on-box checks pending)
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
- **Built:** `browser` (playwright-mcp `v0.0.82`, pinned by tag and digest, `--isolated`,
  `--proxy-server=http://egress:3128`, `--allowed-hosts=browser:8931`, `--no-webmcp`,
  `--image-responses=omit`, no ports or env, only its read-only launch config mounted, `cap_drop: ALL`) on the `internal: true`
  `browser` network, which only `api` and `egress` also join; `egress` (`deploy/Dockerfile.egress`
  + `deploy/egress/squid.conf`) on `browser` + `browser_out` only. `test_browser_compose.py`
  pins the membership, the flags, the pins, and that every compose service name is in the
  proxy's deny list. CI builds the proxy image. `POST /api/debug/browse` (a job, polled at
  `/jobs/{id}`, `web.browse` scope) and `debug-connect.sh browse` return the step trace, the
  answer, whether it was verified, the final URL and the exact text jerv would read; `--spec`
  runs one goal on a chosen model for the bake-off. `/api/debug/fetch` now also reports `gated`.
- **Hardened after review (2026-10-05):** the browser runs `read_only` with size-capped tmpfs
  for `/tmp` (profile + MCP output, `--output-max-size`) and `/home/node`, so nothing a page
  does can fill the box's disk; WebRTC may not send UDP around the proxy (a Chromium flag via
  `deploy/browser/config.json`, verified applied on the real image); Squid caps a plain-HTTP
  reply at 50 MB and also refuses `.localhost`, 6to4 and Teredo. CI now RUNS the built proxy
  and asserts its 403 matrix (`deploy/egress/fence-check.sh` — verified locally against the
  real image: 20 refusals, a public site through).
- **Deferred:** `reader`/`byparr` behind the proxy (see §2). Ops does not yet group the two
  new containers (they show under "Other") — B2's Ops work.
- **Pending on-box (owner notice):** the fence list above, through `debug-connect.sh browse`
  (the host gate refuses literal private addresses and internal names before the browser sees
  them, so the proxy's own refusal is exercised with a public name that resolves privately, a
  redirect, and a rebinding host); the injection canary; the Epic Theatres goal.

### B1 — The `browse` sub-agent 🟡 (built 2026-10-05; bake-off pending)
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
- **Built:** `agent/browse.py` (the loop), `agent/browse_policy.py` (the gate, the page view,
  the quarantine), `agent/browse_actions/*.tool` (the eleven actions the model may pick:
  navigate, click, type_text, select_option, press_key, go_back, wait_for, tabs, snapshot,
  finish, give_up — mapped onto eight playwright-mcp tools plus `browser_snapshot`; nothing
  else on the server is reachable), `prompts/browse.prompt`, and jerv's `browse` tool
  (`tools/browse.tool`, `agent/browsetools.py`) with jerv prompt guidance (`agent-jerv-v56`).
  Budgets: 20 steps (ceiling 30), 240 s, 12 distinct pages, the same action refused on the
  third try and stopping the run on the fourth, six actions in a row that leave the page
  unchanged stop it. `finish` must quote evidence the host finds on the page as it is NOW;
  one miss is sent back, a second is accepted but returned to jerv marked UNVERIFIED.
- **The interim action rule** (until B2's risk gate): typing and selecting only into search,
  filter, location and date fields (or anything inside the page's `search` landmark) — no bare
  "address", "state", "type" or "format" — never a field whose label names an email, password,
  phone, card, account, name, message or code, and never a value shaped like an email or a long
  number; `Enter` only after such a field on the same page took the text; buttons that buy,
  book, sign in, send, submit, continue, proceed or go "next" are refused; links always pass
  (navigation is a GET). Numeric host spellings (`127.1`, octal, hex, one big decimal) and
  CGNAT are refused before the browser sees them. Dialogs and file choosers are dismissed by
  the HOST; the model is never offered either. One run at a time (a semaphore shared by jerv
  and the debug route).
- **The result jerv reads** is host lines plus the answer, last, on ONE line between
  `<<<BROWSE ANSWER BEGIN>>>`/`END>>>` markers (which are stripped from the answer), so a page
  cannot forge "Outcome:" or "Checked:" lines; error text and URLs are sanitized too (a URL that
  could carry a line break or hidden text is dropped). `finish` evidence must be at least 20
  characters or three words.
- **web_fetch hand-off:** the 200-character bar is kept for recovery, but a first page whose
  wording is a location/store picker (`fetch.looks_like_location_gate`, under 1,500 chars) is
  now flagged `gated` whatever its length, says so, and — for a caller holding `browse` —
  suggests it, as an unrendered JS shell now does too.
- **First live run, and the step-cost fix (2026-10-05).** jerv's browse for "Epic Titusville
  showtimes tonight" on Flash-Next (`qwen3.8-flash-next`, research slot) ran 8 steps and hit
  the 240 s wall with outcome `timeout` and NO answer — while its final page was the
  showtimes page. Per-step `browse.step` input grew 2.7k → 6.8k → 7.9k → 6.7k → 13.3k →
  14.5k → 13.5k tokens, at 4, 15, 18, 17, 49, 40 and 58 s, writing 50–631 tokens each; a
  debug run of the same goal took 165 s over 6 steps, its `finish` alone 92 s for 1,443
  tokens. Three causes, three fixes:
  1. **The whole prompt was re-prefilled every step.** The current page sat in the FIRST
     message, so each step's prompt diverged at its start and llama-server (Flash-Next is a
     hybrid; it reuses only a stable prefix) prefilled everything again. Now the messages are
     append-only: the opening is the goal and static instructions, every older page is its
     one-line note, and the page in full rides only on the last message. Step N's messages
     minus the last are a prefix of step N+1's (pinned by a test). That is prefix stability
     at the MESSAGE level; reuse at the TOKEN level is **unmeasured**. The prompt still
     diverges where the previous page (in full last step, a one-line note now) begins, and
     on the hybrid a cache can only be resumed from a context checkpoint at or before that
     point: the end-of-prompt checkpoint sits after it, and the mid-prefill ones
     (`--checkpoint-min-step 1024`, 8 per slot) can be evicted by a big page's own prefill.
     The research slot is shared, so concurrent research tasks can evict it too. Check it on
     the box: the debug `/browse` step trace now reports each step's `prompt_tokens`,
     `cached_tokens` and `output_tokens` (from `LlmUsage`), and llama-server's log says
     "restored context checkpoint" or "forcing full prompt re-processing" per request.
  2. **Every click thought at `xhigh`.** `browse.step` sat in the medium bucket, which sends
     no level, and Qwen3.8's template then defaults to `xhigh`. It is now in the **low**
     bucket (as `research.title`, which also follows the chat model, is), and — though it has
     no picker row — it is listed by name on the Flash-Next reasoning card so the owner can
     raise it; the `low` tier's level applies too. On a cloud `agent.turn` (Grok) the step
     now sends `reasoning_effort=low` as well — intended: a click needs no deep thought on
     any model. The prompt (`agent-browse-v2`) asks for a
     compact answer (a short list, under ~150 words) and to finish at once when the page
     already answers; once two steps or 60 s remain, the page carries a note to finish now.
     `STEP_MAX_TOKENS` (4,096) is left as is: at low effort a step's reply is the tool call.
  3. **A stop threw the page away.** A run that ends on `timeout`, `step_budget`,
     `page_budget`, `no_action`, `loop` or `stuck` now returns its final page's text
     (quarantined like the answer, capped at 6,000 chars, one line between
     `<<<BROWSE PAGE TEXT BEGIN/END>>>` markers after the host lines, labelled UNVERIFIED),
     with the final URL — that page's own, blank when it reported none, never an earlier
     page's — so jerv can answer from it (`browse.tool` v2 says so). The forgery test covers
     this path. `gave_up` and `error` carry none. The quarantine now NFKC-folds and strips
     every Unicode format (Cf) character — zero-width, bidi, word joiners, BOM, the Tag
     block — plus variation selectors, before its address check; marker stripping folds
     too and repeats until no marker is left, so fullwidth, split and nested markers cannot
     pose as an END line (regression tests for each).
  The wall stays at 240 s: with cache reuse and low effort a step should cost seconds, not a
  minute, and a run that still runs out now hands back the page. Re-measure on the box.
- **Pending:** the bake-off is an on-box measurement and has not been run; agent-browser is
  untested. The snapshot pruning measured ~3x on Epic's home page (9.0k → 2.8k chars) and
  ~1.4k tokens on wikipedia.org against the real image.

### B2 — Seeing it in chat, and approving ◻️ (GUI gate: three mocks, owner picks)
- A **tool card** in the chat that pops up while `browse` runs (owner decision 2026-10-04:
  screenshots, no live stream): the latest screenshot and readable step rows ("Opened
  epictheatres.com", "Picked 'Titusville'", "Read today's showtimes"); it settles into the
  Worked pane when the run ends.
- The **approval card** for the Ask tier, in the same place: screenshot, site, the fields
  and values about to be submitted, Allow / Deny / Always allow on this site.
- The risk gate (§2a) ships here with its rules, Presidio on typed values, and the judge
  call; its decisions are logged per step.
- Settings: browsing on/off, step budget. Ops: the browser container's health and the last
  runs' traces.
- **Close the api hop** (the accepted risk in §2): put an MCP-only relay between the api and
  the browser, or a network shape where the api reaches the browser without the browser being
  able to reach the api, so a compromised Chromium has nothing on its network but the proxy.

### B3 — Search off Tavily ◻️
- **Done ahead of the plan (PR #1557):** SearXNG, reader and byparr pinned to dated/digest
  images — the update never re-pulled `latest`, so all three had frozen at install.
- SearXNG settings: enable Bing and the structured engines (Wikipedia, Wikidata, GitHub,
  StackExchange, arXiv); drop or down-weight DuckDuckGo (it bans the IP); shorten
  `suspended_times` (defaults bench an engine for a day on one captcha); surface engine
  suspensions on Ops.
- A rate-limited queue in front of SearXNG for deep-research bursts, on top of the
  existing one-hour cache.
- Provider chain with per-provider budgets: SearXNG → **Brave Search API** (owner decision
  2026-10-04; about 1,000 queries/month on its free credit, then $5/1,000) → optionally
  Mojeek (£2/1,000). Tavily becomes an optional last tier the owner can switch off.
- The Brave key is set **in Settings**, never `.env` — a panel mirroring the Tavily one
  (`api/tavily_settings.py`): stored key with the env as fallback, enable toggle, a "Test
  key" button, and this month's usage against the budget.
- **Brave provider and Settings panel landed in PR #1557:** `BraveSearch` + a per-month query
  budget (default 900, counted in `app.settings`), the chain reordered to SearXNG → Brave →
  Tavily, and the **Brave Search** Settings panel (`api/brave_settings.py`). The rest of B3
  is still open.
- A week of per-engine health on the new pin (including a 50-query burst) decides whether
  Brave's free credit covers the overflow.

## 4. Decided (owner, 2026-10-04)
1. **Forms are allowed, behind approval** — the tiered risk gate in §2a.
2. **Screenshots, no live view** — shown as a tool card that pops up during a run.
3. **Brave Search API is in**, its key set in Settings.

## 5. Open questions
1. **The judge's model.** Flash-Next itself at low effort in a small slot, or a smaller
   dedicated classifier? Decided by B2's measured false-auto rate.
